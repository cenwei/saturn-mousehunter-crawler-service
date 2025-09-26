"""
Saturn MouseHunter 爬虫服务优雅关闭管理器
支持K8s环境下的零停机部署和任务不丢失
"""
import os
import signal
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass

from saturn_mousehunter_shared.log.logger import get_logger
from saturn_mousehunter_shared.dragonfly.dragonfly_client import DragonflyClient
from .dragonfly_task_consumer import DragonflyTaskConsumer, TaskExecution, TaskStatus

log = get_logger(__name__)


@dataclass
class ShutdownConfig:
    """关闭配置"""
    max_wait_seconds: int = 90          # 最大等待时间
    task_requeue_timeout: int = 30      # 任务重新入队超时
    cleanup_timeout: int = 15           # 资源清理超时
    force_exit_delay: int = 5           # 强制退出延迟


class GracefulShutdownManager:
    """优雅关闭管理器

    负责处理K8s环境下的SIGTERM信号，确保：
    1. 停止接收新任务
    2. 等待正在执行的任务完成
    3. 未完成任务重新入队
    4. 清理资源并安全退出
    """

    def __init__(
        self,
        task_consumer: DragonflyTaskConsumer,
        config: Optional[ShutdownConfig] = None
    ):
        self.task_consumer = task_consumer
        self.config = config or ShutdownConfig()
        self.shutdown_in_progress = False
        self.shutdown_start_time: Optional[datetime] = None

        # 注册信号处理器
        self._register_signal_handlers()

        log.info("优雅关闭管理器已初始化",
                max_wait_seconds=self.config.max_wait_seconds,
                worker_id=self.task_consumer.worker_config.worker_id)

    def _register_signal_handlers(self):
        """注册系统信号处理器"""
        try:
            # SIGTERM - K8s发送的优雅关闭信号
            signal.signal(signal.SIGTERM, self._handle_shutdown_signal)
            # SIGINT - Ctrl+C信号
            signal.signal(signal.SIGINT, self._handle_shutdown_signal)

            log.info("信号处理器注册成功")

        except Exception as e:
            log.error("信号处理器注册失败", error=str(e))

    def _handle_shutdown_signal(self, signum: int, frame):
        """处理关闭信号"""
        signal_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        log.info(f"收到{signal_name}信号，开始优雅关闭流程...",
                signal=signal_name,
                worker_id=self.task_consumer.worker_config.worker_id)

        # 异步启动关闭流程
        asyncio.create_task(self.graceful_shutdown())

    async def graceful_shutdown(self):
        """优雅关闭主流程"""
        if self.shutdown_in_progress:
            log.warning("优雅关闭已在进行中，忽略重复信号")
            return

        self.shutdown_in_progress = True
        self.shutdown_start_time = datetime.now()

        log.info("开始优雅关闭流程",
                worker_id=self.task_consumer.worker_config.worker_id,
                max_wait_seconds=self.config.max_wait_seconds)

        try:
            # Step 1: 停止接收新任务 (立即生效)
            await self._stop_accepting_new_tasks()

            # Step 2: 等待正在执行的任务完成
            completed_successfully = await self._wait_for_active_tasks_completion()

            # Step 3: 处理未完成的任务
            if not completed_successfully:
                await self._handle_incomplete_tasks()

            # Step 4: 清理资源
            await self._cleanup_resources()

            # Step 5: 记录关闭统计
            await self._log_shutdown_stats()

            log.info("优雅关闭流程完成",
                    worker_id=self.task_consumer.worker_config.worker_id,
                    duration_seconds=(datetime.now() - self.shutdown_start_time).total_seconds())

        except Exception as e:
            log.error("优雅关闭过程中发生错误", error=str(e))
        finally:
            # 强制退出，确保容器能够终止
            await asyncio.sleep(self.config.force_exit_delay)
            log.info("执行强制退出")
            os._exit(0)

    async def _stop_accepting_new_tasks(self):
        """停止接收新任务"""
        log.info("停止接收新任务...")

        try:
            # 标记不再接受新任务（影响健康检查）
            self.task_consumer.accepting_tasks = False

            # 暂停队列消费
            await self.task_consumer.pause_consumption()

            # 从Worker注册表中注销
            await self.task_consumer._unregister_worker()

            log.info("成功停止接收新任务")

        except Exception as e:
            log.error("停止接收新任务失败", error=str(e))

    async def _wait_for_active_tasks_completion(self) -> bool:
        """等待活跃任务完成

        Returns:
            bool: True表示所有任务都完成了，False表示超时后仍有未完成任务
        """
        log.info("等待正在执行的任务完成...")

        wait_time = 0
        check_interval = 5  # 每5秒检查一次

        while (self.task_consumer.active_executions and
               wait_time < self.config.max_wait_seconds):

            active_count = len(self.task_consumer.active_executions)
            remaining_time = self.config.max_wait_seconds - wait_time

            log.info(f"等待 {active_count} 个任务完成",
                    active_tasks=active_count,
                    wait_time=wait_time,
                    remaining_time=remaining_time)

            # 检查是否有任务已超时，提前处理
            await self._check_and_handle_timeout_tasks()

            await asyncio.sleep(check_interval)
            wait_time += check_interval

        final_active_count = len(self.task_consumer.active_executions)

        if final_active_count == 0:
            log.info("所有任务已完成")
            return True
        else:
            log.warning(f"等待超时，仍有 {final_active_count} 个任务未完成",
                       active_tasks=final_active_count)
            return False

    async def _check_and_handle_timeout_tasks(self):
        """检查并处理超时任务"""
        current_time = datetime.now()
        timeout_executions = []

        for execution_id, execution in self.task_consumer.active_executions.items():
            if current_time > execution.timeout_at:
                timeout_executions.append((execution_id, execution))

        if timeout_executions:
            log.warning(f"发现 {len(timeout_executions)} 个超时任务")

            for execution_id, execution in timeout_executions:
                try:
                    # 标记任务为超时
                    await self.task_consumer._update_task_status(
                        execution.task.task_id,
                        TaskStatus.TIMEOUT,
                        {
                            "reason": "task_timeout_during_shutdown",
                            "execution_id": execution_id,
                            "timeout_at": execution.timeout_at.isoformat()
                        }
                    )

                    # 从活跃执行中移除
                    self.task_consumer.active_executions.pop(execution_id, None)

                    log.info("超时任务已处理",
                            task_id=execution.task.task_id,
                            execution_id=execution_id)

                except Exception as e:
                    log.error("处理超时任务失败",
                             task_id=execution.task.task_id,
                             error=str(e))

    async def _handle_incomplete_tasks(self):
        """处理未完成的任务"""
        incomplete_count = len(self.task_consumer.active_executions)

        if incomplete_count == 0:
            return

        log.warning(f"处理 {incomplete_count} 个未完成任务...")

        requeue_success = 0
        requeue_failed = 0

        # 创建未完成任务列表的副本，避免在迭代时修改
        incomplete_executions = list(self.task_consumer.active_executions.items())

        for execution_id, execution in incomplete_executions:
            try:
                # 重新入队任务
                success = await self._requeue_task(execution)

                if success:
                    requeue_success += 1
                    # 更新任务状态
                    await self.task_consumer._update_task_status(
                        execution.task.task_id,
                        TaskStatus.PENDING_RETRY,
                        {
                            "reason": "worker_graceful_shutdown",
                            "execution_id": execution_id,
                            "retry_count": execution.retry_count + 1,
                            "requeued_at": datetime.now().isoformat()
                        }
                    )
                else:
                    requeue_failed += 1
                    # 标记为失败
                    await self.task_consumer._update_task_status(
                        execution.task.task_id,
                        TaskStatus.FAILED,
                        {
                            "reason": "requeue_failed_during_shutdown",
                            "execution_id": execution_id,
                            "error": "failed to requeue task"
                        }
                    )

                # 从活跃执行中移除
                self.task_consumer.active_executions.pop(execution_id, None)

            except Exception as e:
                log.error("处理未完成任务失败",
                         task_id=execution.task.task_id,
                         execution_id=execution_id,
                         error=str(e))
                requeue_failed += 1

        log.info("未完成任务处理完成",
                requeue_success=requeue_success,
                requeue_failed=requeue_failed,
                total_incomplete=incomplete_count)

    async def _requeue_task(self, execution: TaskExecution) -> bool:
        """重新入队单个任务

        Args:
            execution: 任务执行上下文

        Returns:
            bool: 重新入队是否成功
        """
        try:
            # 准备重新入队的任务数据
            requeue_data = execution.task.task_data.copy()
            requeue_data.update({
                "retry_count": execution.retry_count + 1,
                "original_execution_id": execution.execution_id,
                "requeued_by": execution.worker_id,
                "requeue_reason": "worker_graceful_shutdown"
            })

            # 重新入队
            await self.task_consumer.dragonfly_client.enqueue_task(
                queue_name=execution.task.queue_name,
                task_data=requeue_data
            )

            log.info("任务重新入队成功",
                    task_id=execution.task.task_id,
                    queue_name=execution.task.queue_name,
                    retry_count=execution.retry_count + 1)

            return True

        except Exception as e:
            log.error("任务重新入队失败",
                     task_id=execution.task.task_id,
                     error=str(e))
            return False

    async def _cleanup_resources(self):
        """清理资源"""
        log.info("开始清理资源...")

        try:
            # 超时保护
            async with asyncio.timeout(self.config.cleanup_timeout):
                # 停止任务消费者
                await self.task_consumer.stop()

                # 清理其他资源
                # 注意：数据库连接等资源的清理应该在各自的cleanup方法中处理

            log.info("资源清理完成")

        except asyncio.TimeoutError:
            log.warning("资源清理超时",
                       timeout_seconds=self.config.cleanup_timeout)
        except Exception as e:
            log.error("资源清理失败", error=str(e))

    async def _log_shutdown_stats(self):
        """记录关闭统计信息"""
        if not self.shutdown_start_time:
            return

        shutdown_duration = (datetime.now() - self.shutdown_start_time).total_seconds()

        stats = {
            "worker_id": self.task_consumer.worker_config.worker_id,
            "shutdown_duration_seconds": shutdown_duration,
            "final_active_tasks": len(self.task_consumer.active_executions),
            "shutdown_start_time": self.shutdown_start_time.isoformat(),
            "shutdown_end_time": datetime.now().isoformat(),
            "max_wait_configured": self.config.max_wait_seconds
        }

        log.info("优雅关闭统计", **stats)

    def is_shutting_down(self) -> bool:
        """检查是否正在关闭中"""
        return self.shutdown_in_progress