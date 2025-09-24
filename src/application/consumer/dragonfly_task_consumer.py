"""
Dragonfly任务队列消费者模块
实现从Dragonfly队列消费任务的下游爬虫微服务组件
"""

import asyncio
from typing import Dict, Any, Optional, List, Callable
from datetime import datetime
from dataclasses import dataclass
from enum import Enum

from saturn_mousehunter_shared.log.logger import get_logger
from saturn_mousehunter_shared.mq.dragonfly_client import DragonflyClient
from saturn_mousehunter_shared.mq.message_types import DragonflyTask, QueuePriority

log = get_logger(__name__)


class TaskStatus(Enum):
    """任务状态枚举"""
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"


@dataclass
class WorkerConfig:
    """工作器配置"""
    worker_id: str
    max_concurrent_tasks: int = 5
    task_timeout_seconds: int = 300
    supported_task_types: List[str] = None
    supported_markets: List[str] = None
    queue_priorities: List[QueuePriority] = None

    def __post_init__(self):
        if self.supported_task_types is None:
            self.supported_task_types = ["1m_realtime", "5m_realtime", "15m_realtime", "15m_backfill", "1d_backfill"]
        if self.supported_markets is None:
            self.supported_markets = ["CN", "US", "HK"]
        if self.queue_priorities is None:
            self.queue_priorities = [QueuePriority.CRITICAL, QueuePriority.HIGH, QueuePriority.NORMAL, QueuePriority.LOW]


@dataclass
class TaskExecution:
    """任务执行上下文"""
    task: DragonflyTask
    worker_id: str
    start_time: datetime
    timeout_seconds: int = 300


class CrawlerTaskConsumer:
    """Dragonfly任务队列消费者"""

    def __init__(
        self,
        crawler_engine,  # Forward reference to avoid circular imports
        worker_id: str,
        max_concurrent_tasks: int = 5,
        task_timeout_seconds: int = 300,
        supported_task_types: Optional[List[str]] = None,
        supported_markets: Optional[List[str]] = None
    ):
        self.crawler_engine = crawler_engine
        self.worker_config = WorkerConfig(
            worker_id=worker_id,
            max_concurrent_tasks=max_concurrent_tasks,
            task_timeout_seconds=task_timeout_seconds,
            supported_task_types=supported_task_types,
            supported_markets=supported_markets
        )

        self.dragonfly_client: Optional[DragonflyClient] = None
        self.running = False
        self.consumer_tasks: List[asyncio.Task] = []
        self.active_executions: Dict[str, TaskExecution] = {}

        # 任务处理器注册表
        self.task_handlers: Dict[str, Callable] = {}

        # 统计信息
        self.stats = {
            "tasks_consumed": 0,
            "tasks_completed": 0,
            "tasks_failed": 0,
            "tasks_timeout": 0,
            "retry_tasks": 0,
            "last_task_time": None
        }

    async def initialize(self) -> bool:
        """初始化消费者"""
        try:
            # 创建Dragonfly客户端
            self.dragonfly_client = DragonflyClient(
                service_name="saturn-mousehunter-crawler-service",
                host="192.168.8.188",
                port=30010,
                db=0
            )

            # 确保DragonflyClient已初始化
            if not self.dragonfly_client._redis:
                await self.dragonfly_client.initialize()

            # 注册工作器
            await self._register_worker()

            log.info("Dragonfly任务消费者初始化成功",
                    worker_id=self.worker_config.worker_id,
                    max_concurrent=self.worker_config.max_concurrent_tasks,
                    task_timeout=self.worker_config.task_timeout_seconds,
                    supported_types=self.worker_config.supported_task_types,
                    supported_markets=self.worker_config.supported_markets)
            return True

        except Exception as e:
            log.error("Dragonfly任务消费者初始化失败", error=str(e))
            return False

    async def start(self):
        """启动消费者"""
        if self.running:
            log.warning("任务消费者已经在运行中", worker_id=self.worker_config.worker_id)
            return

        self.running = True

        # 为每个优先级启动消费者任务
        for priority in self.worker_config.queue_priorities:
            consumer_task = asyncio.create_task(
                self._consume_priority_queue(priority)
            )
            self.consumer_tasks.append(consumer_task)

        # 启动延迟任务处理器
        delayed_task = asyncio.create_task(self._process_delayed_tasks())
        self.consumer_tasks.append(delayed_task)

        # 启动工作器状态上报
        status_task = asyncio.create_task(self._report_worker_status())
        self.consumer_tasks.append(status_task)

        log.info("Dragonfly任务消费者启动成功",
                worker_id=self.worker_config.worker_id,
                priorities=[p.value for p in self.worker_config.queue_priorities])

    async def stop(self):
        """停止消费者"""
        if not self.running:
            return

        self.running = False

        # 取消所有消费者任务
        for task in self.consumer_tasks:
            if not task.done():
                task.cancel()

        # 等待所有任务完成
        await asyncio.gather(*self.consumer_tasks, return_exceptions=True)
        self.consumer_tasks.clear()

        # 等待活跃执行完成（设置超时）
        if self.active_executions:
            log.info("等待活跃任务完成",
                    active_count=len(self.active_executions),
                    worker_id=self.worker_config.worker_id)

            await asyncio.sleep(2)  # 给正在执行的任务一些时间完成

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
        log.info("任务处理器已注册",
                task_type=task_type,
                worker_id=self.worker_config.worker_id)

    async def _consume_priority_queue(self, priority: QueuePriority):
        """消费指定优先级队列"""
        log.info("启动优先级队列消费者",
                worker_id=self.worker_config.worker_id,
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

                # 检查任务类型是否支持
                if task.task_type not in self.worker_config.supported_task_types:
                    log.warning("不支持的任务类型，重新入队",
                               task_id=task.task_id,
                               task_type=task.task_type,
                               supported_types=self.worker_config.supported_task_types)

                    # 重新入队到low优先级
                    task.priority = QueuePriority.LOW
                    await self.dragonfly_client.enqueue_task(task, delay_seconds=60)
                    continue

                # 检查市场是否支持
                if task.market not in self.worker_config.supported_markets:
                    log.warning("不支持的市场，重新入队",
                               task_id=task.task_id,
                               market=task.market,
                               supported_markets=self.worker_config.supported_markets)

                    task.priority = QueuePriority.LOW
                    await self.dragonfly_client.enqueue_task(task, delay_seconds=60)
                    continue

                # 创建任务执行上下文
                execution = TaskExecution(
                    task=task,
                    worker_id=self.worker_config.worker_id,
                    start_time=datetime.now(),
                    timeout_seconds=self.worker_config.task_timeout_seconds
                )

                # 添加到活跃执行列表
                self.active_executions[task.task_id] = execution

                # 异步执行任务
                asyncio.create_task(self._execute_task(execution))

                # 更新统计
                self.stats["tasks_consumed"] += 1
                self.stats["last_task_time"] = datetime.now()

                log.info("任务已分配给执行器",
                        task_id=task.task_id,
                        task_type=task.task_type,
                        priority=priority.value,
                        active_executions=len(self.active_executions))

            except Exception as e:
                log.error("队列消费过程出错",
                         worker_id=self.worker_config.worker_id,
                         priority=priority.value,
                         error=str(e))
                await asyncio.sleep(1)

    async def _execute_task(self, execution: TaskExecution):
        """执行单个任务"""
        task = execution.task
        task_id = task.task_id

        try:
            # 更新任务状态为运行中
            await self.dragonfly_client.update_task_status(
                task_id, TaskStatus.RUNNING.value, {
                    "worker_id": execution.worker_id,
                    "start_time": execution.start_time.isoformat()
                }
            )

            # 查找任务处理器
            handler = self.task_handlers.get(task.task_type)
            if not handler:
                # 使用默认爬虫引擎处理
                handler = self.crawler_engine.execute_crawling_task

            # 执行任务（带超时）
            try:
                success = await asyncio.wait_for(
                    handler(task),
                    timeout=execution.timeout_seconds
                )

                if success:
                    await self.dragonfly_client.update_task_status(
                        task_id, TaskStatus.COMPLETED.value, {
                            "completed_time": datetime.now().isoformat(),
                            "execution_duration": (datetime.now() - execution.start_time).total_seconds()
                        }
                    )
                    self.stats["tasks_completed"] += 1
                    log.info("任务执行成功",
                            task_id=task_id,
                            task_type=task.task_type,
                            duration=(datetime.now() - execution.start_time).total_seconds())
                else:
                    await self._handle_task_failure(task, execution, "Task execution returned False")

            except asyncio.TimeoutError:
                await self._handle_task_timeout(task, execution)

        except Exception as e:
            await self._handle_task_failure(task, execution, str(e))

        finally:
            # 从活跃执行列表中移除
            self.active_executions.pop(task_id, None)

    async def _handle_task_failure(self, task: DragonflyTask, execution: TaskExecution, error: str):
        """处理任务失败"""
        task_id = task.task_id

        log.error("任务执行失败",
                 task_id=task_id,
                 task_type=task.task_type,
                 error=error,
                 retry_count=task.retry_count,
                 max_retries=task.max_retries)

        # 检查是否需要重试
        if task.retry_count < task.max_retries:
            task.retry_count += 1

            # 计算延迟时间（指数退避）
            delay_seconds = min(60 * (2 ** (task.retry_count - 1)), 300)

            # 重新入队
            await self.dragonfly_client.enqueue_task(task, delay_seconds=delay_seconds)

            self.stats["retry_tasks"] += 1
            log.info("任务已重新入队",
                    task_id=task_id,
                    retry_count=task.retry_count,
                    delay_seconds=delay_seconds)
        else:
            # 达到最大重试次数，标记为失败
            await self.dragonfly_client.update_task_status(
                task_id, TaskStatus.FAILED.value, {
                    "error": error,
                    "failed_time": datetime.now().isoformat(),
                    "final_retry_count": task.retry_count
                }
            )
            self.stats["tasks_failed"] += 1

    async def _handle_task_timeout(self, task: DragonflyTask, execution: TaskExecution):
        """处理任务超时"""
        task_id = task.task_id

        log.warning("任务执行超时",
                   task_id=task_id,
                   task_type=task.task_type,
                   timeout_seconds=execution.timeout_seconds,
                   retry_count=task.retry_count)

        # 超时也按失败处理，但使用不同的延迟策略
        if task.retry_count < task.max_retries:
            task.retry_count += 1

            # 超时任务使用更长的延迟
            delay_seconds = 300  # 5分钟后重试

            await self.dragonfly_client.enqueue_task(task, delay_seconds=delay_seconds)

            log.info("超时任务已重新入队",
                    task_id=task_id,
                    retry_count=task.retry_count,
                    delay_seconds=delay_seconds)
        else:
            await self.dragonfly_client.update_task_status(
                task_id, TaskStatus.TIMEOUT.value, {
                    "timeout_seconds": execution.timeout_seconds,
                    "failed_time": datetime.now().isoformat()
                }
            )

        self.stats["tasks_timeout"] += 1

    async def _process_delayed_tasks(self):
        """处理延迟任务"""
        while self.running:
            try:
                await self.dragonfly_client.process_delayed_tasks()
                await asyncio.sleep(30)  # 每30秒检查一次延迟任务
            except Exception as e:
                log.error("处理延迟任务失败",
                         worker_id=self.worker_config.worker_id,
                         error=str(e))
                await asyncio.sleep(60)

    async def _register_worker(self):
        """注册工作器"""
        worker_info = {
            "worker_id": self.worker_config.worker_id,
            "service": "saturn-mousehunter-crawler-service",
            "max_concurrent_tasks": self.worker_config.max_concurrent_tasks,
            "supported_task_types": self.worker_config.supported_task_types,
            "supported_markets": self.worker_config.supported_markets,
            "queue_priorities": [p.value for p in self.worker_config.queue_priorities],
            "registered_at": datetime.now().isoformat()
        }

        await self.dragonfly_client.cache_set(
            f"worker:{self.worker_config.worker_id}",
            worker_info,
            expire_seconds=3600  # 1小时过期
        )

    async def _unregister_worker(self):
        """注销工作器"""
        await self.dragonfly_client.cache_delete(f"worker:{self.worker_config.worker_id}")

    async def _report_worker_status(self):
        """上报工作器状态"""
        while self.running:
            try:
                status_info = {
                    "worker_id": self.worker_config.worker_id,
                    "running": self.running,
                    "active_executions": len(self.active_executions),
                    "stats": self.stats.copy(),
                    "updated_at": datetime.now().isoformat()
                }

                # 转换datetime对象为字符串
                if status_info["stats"]["last_task_time"]:
                    status_info["stats"]["last_task_time"] = status_info["stats"]["last_task_time"].isoformat()

                await self.dragonfly_client.cache_set(
                    f"worker_status:{self.worker_config.worker_id}",
                    status_info,
                    expire_seconds=300  # 5分钟过期
                )

                await asyncio.sleep(60)  # 每分钟上报一次

            except Exception as e:
                log.error("上报工作器状态失败", error=str(e))
                await asyncio.sleep(60)

    async def _update_task_status(self, task_id: str, status: TaskStatus, details: Optional[Dict[str, Any]] = None):
        """更新任务状态"""
        await self.dragonfly_client.update_task_status(task_id, status.value, details)

    def get_stats(self) -> Dict[str, Any]:
        """获取消费者统计信息"""
        stats = self.stats.copy()
        stats.update({
            "worker_id": self.worker_config.worker_id,
            "running": self.running,
            "active_executions": len(self.active_executions),
            "max_concurrent_tasks": self.worker_config.max_concurrent_tasks,
            "queue_priorities": [p.value for p in self.worker_config.queue_priorities]
        })
        return stats

    def get_active_tasks(self) -> List[Dict[str, Any]]:
        """获取活跃任务列表"""
        return [
            {
                "task_id": execution.task.task_id,
                "task_type": execution.task.task_type,
                "market": execution.task.market,
                "symbol": execution.task.symbol,
                "start_time": execution.start_time.isoformat(),
                "duration": (datetime.now() - execution.start_time).total_seconds()
            }
            for execution in self.active_executions.values()
        ]