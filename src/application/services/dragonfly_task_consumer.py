"""
Dragonfly任务队列消费者模块
实现从Dragonfly队列消费任务的下游爬虫微服务组件
"""

import asyncio
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Callable, Any
from dataclasses import dataclass
from enum import Enum

from saturn_mousehunter_shared.log.logger import get_logger
from saturn_mousehunter_shared.mq import DragonflyClient
from saturn_mousehunter_shared.mq.message_types import DragonflyTask, QueuePriority

log = get_logger(__name__)


class TaskStatus(str, Enum):
    """任务状态枚举"""
    QUEUED = "QUEUED"        # 已入队
    ASSIGNED = "ASSIGNED"    # 已分配
    RUNNING = "RUNNING"      # 执行中
    SUCCESS = "SUCCESS"      # 成功
    FAILED = "FAILED"        # 失败
    RETRY = "RETRY"          # 重试中
    TIMEOUT = "TIMEOUT"      # 超时
    CANCELLED = "CANCELLED"  # 已取消


@dataclass
class WorkerConfig:
    """工作器配置"""
    worker_id: str                                    # 工作器ID
    max_concurrent_tasks: int = 5                    # 最大并发任务数
    task_timeout_seconds: int = 300                  # 任务超时时间
    queue_priorities: List[QueuePriority] = None     # 监听的队列优先级
    supported_task_types: List[str] = None           # 支持的任务类型
    supported_markets: List[str] = None              # 支持的市场类型


@dataclass
class TaskExecution:
    """任务执行上下文"""
    task: DragonflyTask
    worker_id: str
    start_time: datetime
    timeout_at: datetime
    execution_id: str
    retry_count: int = 0


class DragonflyTaskConsumer:
    """Dragonfly任务队列消费者"""

    def __init__(
        self,
        dragonfly_client: DragonflyClient,
        worker_config: WorkerConfig
    ):
        self.dragonfly_client = dragonfly_client
        self.worker_config = worker_config
        self.running = False
        self.consumer_tasks: List[asyncio.Task] = []
        self.active_executions: Dict[str, TaskExecution] = {}

        # 任务处理器注册表
        self.task_handlers: Dict[str, Callable] = {}

        # 统计信息
        self.stats = {
            "consumed_tasks": 0,
            "successful_tasks": 0,
            "failed_tasks": 0,
            "timeout_tasks": 0,
            "retry_tasks": 0,
            "start_time": None,
            "last_task_time": None
        }

        # 默认队列优先级
        if not worker_config.queue_priorities:
            worker_config.queue_priorities = [
                QueuePriority.CRITICAL,
                QueuePriority.HIGH,
                QueuePriority.NORMAL,
                QueuePriority.LOW
            ]

    async def initialize(self) -> bool:
        """初始化消费者"""
        try:
            # 确保DragonflyClient已初始化
            if not self.dragonfly_client._redis:
                await self.dragonfly_client.initialize()

            # 注册工作器
            await self._register_worker()

            log.info("Dragonfly任务消费者初始化成功",
                    worker_id=self.worker_config.worker_id,
                    max_concurrent=self.worker_config.max_concurrent_tasks,
                    supported_types=self.worker_config.supported_task_types,
                    supported_markets=self.worker_config.supported_markets)

            return True

        except Exception as e:
            log.error("Dragonfly任务消费者初始化失败", error=str(e))
            return False

    async def start(self):
        """启动队列消费者"""
        if self.running:
            log.warning("任务消费者已在运行中")
            return

        self.running = True
        self.stats["start_time"] = datetime.now()

        # 启动多个消费者协程(按优先级)
        for priority in self.worker_config.queue_priorities:
            consumer_task = asyncio.create_task(
                self._consume_queue(priority)
            )
            self.consumer_tasks.append(consumer_task)

        # 启动超时检查协程
        timeout_task = asyncio.create_task(self._timeout_monitor())
        self.consumer_tasks.append(timeout_task)

        # 启动状态上报协程
        status_task = asyncio.create_task(self._status_reporter())
        self.consumer_tasks.append(status_task)

        log.info("Dragonfly任务消费者启动成功",
                worker_id=self.worker_config.worker_id,
                priorities=[p.value for p in self.worker_config.queue_priorities])

    async def stop(self):
        """停止队列消费者"""
        if not self.running:
            return

        self.running = False

        # 取消所有消费者任务
        for task in self.consumer_tasks:
            task.cancel()

        # 等待任务完成
        if self.consumer_tasks:
            await asyncio.gather(*self.consumer_tasks, return_exceptions=True)

        # 清理活跃执行
        for execution_id, execution in self.active_executions.items():
            await self._update_task_status(
                execution.task.task_id,
                TaskStatus.CANCELLED,
                {"reason": "worker_shutdown", "execution_id": execution_id}
            )

        self.active_executions.clear()
        self.consumer_tasks.clear()

        # 注销工作器
        await self._unregister_worker()

        log.info("Dragonfly任务消费者已停止",
                worker_id=self.worker_config.worker_id)

    def register_task_handler(self, task_type: str, handler: Callable):
        """
        注册任务处理器

        Args:
            task_type: 任务类型
            handler: 处理器函数 async def handler(task: DragonflyTask) -> bool
        """
        self.task_handlers[task_type] = handler
        log.info("任务处理器注册成功",
                task_type=task_type,
                worker_id=self.worker_config.worker_id)

    async def _consume_queue(self, priority: QueuePriority):
        """消费指定优先级队列"""
        queue_name = f"crawler_tasks:{priority.value}"

        log.info("开始消费队列",
                worker_id=self.worker_config.worker_id,
                queue=queue_name,
                priority=priority.value)

        while self.running:
            try:
                # 检查并发限制
                if len(self.active_executions) >= self.worker_config.max_concurrent_tasks:
                    await asyncio.sleep(1)
                    continue

                # 从队列获取任务
                task = await self.dragonfly_client.dequeue_task(priority, timeout=5)

                if not task:
                    continue

                # 检查任务类型支持
                if (self.worker_config.supported_task_types and
                    task.task_type not in self.worker_config.supported_task_types):
                    log.warning("不支持的任务类型，重新入队",
                              task_id=task.task_id,
                              task_type=task.task_type,
                              supported_types=self.worker_config.supported_task_types)

                    # 重新入队到low优先级
                    task.priority = QueuePriority.LOW
                    await self.dragonfly_client.enqueue_task(task, delay_seconds=60)
                    continue

                # 检查市场支持
                if (self.worker_config.supported_markets and
                    task.market not in self.worker_config.supported_markets):
                    log.warning("不支持的市场类型，重新入队",
                              task_id=task.task_id,
                              market=task.market,
                              supported_markets=self.worker_config.supported_markets)

                    task.priority = QueuePriority.LOW
                    await self.dragonfly_client.enqueue_task(task, delay_seconds=60)
                    continue

                # 创建执行上下文
                execution = TaskExecution(
                    task=task,
                    worker_id=self.worker_config.worker_id,
                    start_time=datetime.now(),
                    timeout_at=datetime.now() + timedelta(seconds=self.worker_config.task_timeout_seconds),
                    execution_id=f"{self.worker_config.worker_id}_{task.task_id}_{datetime.now().timestamp():.0f}",
                    retry_count=task.retry_count
                )

                # 记录执行
                self.active_executions[execution.execution_id] = execution

                # 异步执行任务
                asyncio.create_task(self._execute_task(execution))

                # 更新统计
                self.stats["consumed_tasks"] += 1
                self.stats["last_task_time"] = datetime.now()

                log.info("任务已分配执行",
                        task_id=task.task_id,
                        execution_id=execution.execution_id,
                        task_type=task.task_type,
                        priority=priority.value)

            except asyncio.CancelledError:
                log.info("队列消费者被取消", queue=queue_name)
                break
            except Exception as e:
                log.error("队列消费出错",
                         queue=queue_name,
                         error=str(e))
                await asyncio.sleep(5)

    async def _execute_task(self, execution: TaskExecution):
        """执行任务"""
        task = execution.task

        try:
            # 更新任务状态为执行中
            await self._update_task_status(
                task.task_id,
                TaskStatus.RUNNING,
                {"execution_id": execution.execution_id, "worker_id": execution.worker_id}
            )

            # 查找任务处理器
            handler = self.task_handlers.get(task.task_type)
            if not handler:
                raise Exception(f"未找到任务类型 {task.task_type} 的处理器")

            # 执行任务
            log.info("开始执行任务",
                    task_id=task.task_id,
                    task_type=task.task_type,
                    execution_id=execution.execution_id)

            success = await handler(task)

            if success:
                # 任务成功
                await self._update_task_status(
                    task.task_id,
                    TaskStatus.SUCCESS,
                    {"execution_id": execution.execution_id, "completed_at": datetime.now().isoformat()}
                )

                self.stats["successful_tasks"] += 1

                log.info("任务执行成功",
                        task_id=task.task_id,
                        execution_id=execution.execution_id,
                        duration=(datetime.now() - execution.start_time).total_seconds())
            else:
                # 任务失败，考虑重试
                await self._handle_task_failure(execution, "Handler returned False")

        except Exception as e:
            # 任务异常
            await self._handle_task_failure(execution, str(e))

        finally:
            # 清理执行上下文
            self.active_executions.pop(execution.execution_id, None)

    async def _handle_task_failure(self, execution: TaskExecution, error_message: str):
        """处理任务失败"""
        task = execution.task

        # 判断是否需要重试
        if task.retry_count < task.max_retries:
            # 重试
            task.retry_count += 1

            # 计算延迟时间(指数退避)
            delay_seconds = min(60 * (2 ** task.retry_count), 3600)  # 最大1小时

            await self._update_task_status(
                task.task_id,
                TaskStatus.RETRY,
                {
                    "execution_id": execution.execution_id,
                    "error": error_message,
                    "retry_count": task.retry_count,
                    "retry_delay_seconds": delay_seconds
                }
            )

            # 重新入队
            await self.dragonfly_client.enqueue_task(task, delay_seconds=delay_seconds)

            self.stats["retry_tasks"] += 1

            log.warning("任务失败，准备重试",
                       task_id=task.task_id,
                       retry_count=task.retry_count,
                       max_retries=task.max_retries,
                       delay_seconds=delay_seconds,
                       error=error_message)
        else:
            # 彻底失败
            await self._update_task_status(
                task.task_id,
                TaskStatus.FAILED,
                {
                    "execution_id": execution.execution_id,
                    "error": error_message,
                    "final_retry_count": task.retry_count
                }
            )

            self.stats["failed_tasks"] += 1

            log.error("任务最终失败",
                     task_id=task.task_id,
                     retry_count=task.retry_count,
                     error=error_message)

    async def _timeout_monitor(self):
        """超时监控器"""
        while self.running:
            try:
                current_time = datetime.now()
                timeout_executions = []

                # 检查超时任务
                for execution_id, execution in self.active_executions.items():
                    if current_time > execution.timeout_at:
                        timeout_executions.append(execution)

                # 处理超时任务
                for execution in timeout_executions:
                    await self._handle_task_timeout(execution)

                await asyncio.sleep(10)  # 每10秒检查一次

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("超时监控器出错", error=str(e))
                await asyncio.sleep(30)

    async def _handle_task_timeout(self, execution: TaskExecution):
        """处理任务超时"""
        task = execution.task

        await self._update_task_status(
            task.task_id,
            TaskStatus.TIMEOUT,
            {
                "execution_id": execution.execution_id,
                "timeout_at": execution.timeout_at.isoformat(),
                "duration_seconds": (datetime.now() - execution.start_time).total_seconds()
            }
        )

        self.stats["timeout_tasks"] += 1

        # 清理执行上下文
        self.active_executions.pop(execution.execution_id, None)

        log.error("任务执行超时",
                 task_id=task.task_id,
                 execution_id=execution.execution_id,
                 timeout_seconds=self.worker_config.task_timeout_seconds)

        # 考虑重试超时任务
        if task.retry_count < task.max_retries:
            task.retry_count += 1
            delay_seconds = 300  # 5分钟后重试

            await self.dragonfly_client.enqueue_task(task, delay_seconds=delay_seconds)

            log.info("超时任务已重新入队",
                    task_id=task.task_id,
                    retry_count=task.retry_count,
                    delay_seconds=delay_seconds)

    async def _status_reporter(self):
        """状态上报器"""
        while self.running:
            try:
                # 上报工作器状态
                await self._report_worker_status()
                await asyncio.sleep(30)  # 每30秒上报一次

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("状态上报器出错", error=str(e))
                await asyncio.sleep(60)

    async def _register_worker(self):
        """注册工作器"""
        worker_info = {
            "worker_id": self.worker_config.worker_id,
            "max_concurrent_tasks": self.worker_config.max_concurrent_tasks,
            "task_timeout_seconds": self.worker_config.task_timeout_seconds,
            "supported_task_types": self.worker_config.supported_task_types or [],
            "supported_markets": self.worker_config.supported_markets or [],
            "queue_priorities": [p.value for p in self.worker_config.queue_priorities],
            "registered_at": datetime.now().isoformat()
        }

        await self.dragonfly_client.cache_set(
            f"worker:{self.worker_config.worker_id}",
            worker_info,
            expire_seconds=120  # 2分钟过期，需要定期更新
        )

    async def _unregister_worker(self):
        """注销工作器"""
        await self.dragonfly_client.cache_delete(f"worker:{self.worker_config.worker_id}")

    async def _report_worker_status(self):
        """上报工作器状态"""
        status_info = {
            "worker_id": self.worker_config.worker_id,
            "running": self.running,
            "active_tasks": len(self.active_executions),
            "max_concurrent_tasks": self.worker_config.max_concurrent_tasks,
            "stats": self.stats.copy(),
            "reported_at": datetime.now().isoformat()
        }

        # 处理datetime序列化
        if status_info["stats"]["start_time"]:
            status_info["stats"]["start_time"] = status_info["stats"]["start_time"].isoformat()
        if status_info["stats"]["last_task_time"]:
            status_info["stats"]["last_task_time"] = status_info["stats"]["last_task_time"].isoformat()

        await self.dragonfly_client.cache_set(
            f"worker_status:{self.worker_config.worker_id}",
            status_info,
            expire_seconds=120
        )

    async def _update_task_status(self, task_id: str, status: TaskStatus, details: Optional[Dict[str, Any]] = None):
        """更新任务状态"""
        await self.dragonfly_client.update_task_status(task_id, status.value, details)

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        current_stats = self.stats.copy()
        current_stats["active_tasks"] = len(self.active_executions)
        current_stats["running"] = self.running

        # 计算运行时间
        if self.stats["start_time"]:
            current_stats["uptime_seconds"] = (datetime.now() - self.stats["start_time"]).total_seconds()

        return current_stats

    async def get_active_tasks(self) -> List[Dict[str, Any]]:
        """获取活跃任务列表"""
        tasks = []
        for execution_id, execution in self.active_executions.items():
            tasks.append({
                "execution_id": execution_id,
                "task_id": execution.task.task_id,
                "task_type": execution.task.task_type,
                "market": execution.task.market,
                "symbol": execution.task.symbol,
                "priority": execution.task.priority.value,
                "start_time": execution.start_time.isoformat(),
                "timeout_at": execution.timeout_at.isoformat(),
                "duration_seconds": (datetime.now() - execution.start_time).total_seconds()
            })
        return tasks