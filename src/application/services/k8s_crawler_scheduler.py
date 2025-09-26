"""
Saturn MouseHunter 爬虫服务 K8s 动态调度器
支持基于队列负载的零停机动态扩缩容
"""
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

try:
    from kubernetes import client, config
    from kubernetes.client.rest import ApiException
    KUBERNETES_AVAILABLE = True
except ImportError:
    KUBERNETES_AVAILABLE = False

from saturn_mousehunter_shared.log.logger import get_logger
from saturn_mousehunter_shared.dragonfly.dragonfly_client import DragonflyClient

log = get_logger(__name__)


class ScalingAction(Enum):
    """扩缩容操作类型"""
    SCALE_UP = "scale_up"
    SCALE_DOWN = "scale_down"
    NO_ACTION = "no_action"


@dataclass
class DeploymentConfig:
    """部署配置"""
    name: str
    namespace: str = "saturn-mousehunter"
    min_replicas: int = 1
    max_replicas: int = 10
    target_queue_depth: int = 50  # 目标队列深度
    scale_up_threshold: int = 80  # 扩容阈值
    scale_down_threshold: int = 20  # 缩容阈值


@dataclass
class QueueMetrics:
    """队列指标"""
    queue_name: str
    depth: int
    consumer_count: int
    processing_rate: float = 0.0  # 每秒处理任务数
    avg_processing_time: float = 0.0  # 平均处理时间(秒)


@dataclass
class ScalingDecision:
    """扩缩容决策"""
    deployment_name: str
    current_replicas: int
    target_replicas: int
    action: ScalingAction
    reason: str
    queue_metrics: QueueMetrics


class K8sCrawlerScheduler:
    """K8s 爬虫动态调度器

    负责根据Dragonfly队列负载情况动态调整爬虫服务的副本数量
    """

    def __init__(
        self,
        dragonfly_client: DragonflyClient,
        namespace: str = "saturn-mousehunter"
    ):
        self.dragonfly_client = dragonfly_client
        self.namespace = namespace
        self.k8s_client: Optional[client.AppsV1Api] = None
        self.last_scaling_actions: Dict[str, datetime] = {}
        self.scaling_cooldown = timedelta(minutes=2)  # 扩缩容冷却时间

        # 部署配置
        self.deployment_configs = {
            "saturn-crawler-critical": DeploymentConfig(
                name="saturn-crawler-critical",
                namespace=namespace,
                min_replicas=2,
                max_replicas=8,
                target_queue_depth=25,
                scale_up_threshold=40,
                scale_down_threshold=10
            ),
            "saturn-crawler-high": DeploymentConfig(
                name="saturn-crawler-high",
                namespace=namespace,
                min_replicas=2,
                max_replicas=10,
                target_queue_depth=50,
                scale_up_threshold=80,
                scale_down_threshold=20
            ),
            "saturn-crawler-normal": DeploymentConfig(
                name="saturn-crawler-normal",
                namespace=namespace,
                min_replicas=1,
                max_replicas=5,
                target_queue_depth=100,
                scale_up_threshold=150,
                scale_down_threshold=30
            )
        }

        # 队列到部署的映射
        self.queue_deployment_mapping = {
            "crawler_backfill_critical": "saturn-crawler-critical",
            "crawler_realtime_critical": "saturn-crawler-critical",
            "crawler_backfill_high": "saturn-crawler-high",
            "crawler_realtime_high": "saturn-crawler-high",
            "crawler_backfill_normal": "saturn-crawler-normal",
            "crawler_realtime_normal": "saturn-crawler-normal"
        }

        self._initialize_k8s_client()

    def _initialize_k8s_client(self):
        """初始化K8s客户端"""
        if not KUBERNETES_AVAILABLE:
            log.warning("kubernetes库未安装，动态调度功能将不可用")
            return

        try:
            # 尝试加载集群内配置
            config.load_incluster_config()
            log.info("已加载K8s集群内配置")
        except config.ConfigException:
            try:
                # 尝试加载本地配置
                config.load_kube_config()
                log.info("已加载K8s本地配置")
            except config.ConfigException:
                log.error("无法加载K8s配置，动态调度功能将不可用")
                return

        self.k8s_client = client.AppsV1Api()
        log.info("K8s动态调度器初始化成功", namespace=self.namespace)

    async def start_monitoring(self, check_interval: int = 30):
        """启动监控和调度循环

        Args:
            check_interval: 检查间隔(秒)
        """
        if not self.k8s_client:
            log.error("K8s客户端未初始化，无法启动监控")
            return

        log.info("启动K8s爬虫动态调度监控",
                check_interval=check_interval,
                namespace=self.namespace)

        while True:
            try:
                await self._monitoring_cycle()
            except Exception as e:
                log.error("监控循环出错", error=str(e))

            await asyncio.sleep(check_interval)

    async def _monitoring_cycle(self):
        """单次监控周期"""
        # 1. 获取队列指标
        queue_metrics = await self._get_queue_metrics()

        if not queue_metrics:
            log.warning("无法获取队列指标，跳过此次调度")
            return

        # 2. 为每个部署做扩缩容决策
        scaling_decisions = []
        for deployment_name, config in self.deployment_configs.items():
            decision = await self._make_scaling_decision(deployment_name, config, queue_metrics)
            if decision:
                scaling_decisions.append(decision)

        # 3. 执行扩缩容操作
        for decision in scaling_decisions:
            if decision.action != ScalingAction.NO_ACTION:
                await self._execute_scaling_decision(decision)

        # 4. 记录监控信息
        if queue_metrics:
            total_queue_depth = sum(m.depth for m in queue_metrics.values())
            log.info("队列监控周期完成",
                    total_queue_depth=total_queue_depth,
                    scaling_actions=len([d for d in scaling_decisions
                                      if d.action != ScalingAction.NO_ACTION]))

    async def _get_queue_metrics(self) -> Dict[str, QueueMetrics]:
        """获取队列指标"""
        metrics = {}

        for queue_name in self.queue_deployment_mapping.keys():
            try:
                # 获取队列深度
                depth = await self.dragonfly_client.get_queue_depth(queue_name)

                # 获取消费者数量（如果支持的话）
                consumer_count = await self._get_queue_consumer_count(queue_name)

                metrics[queue_name] = QueueMetrics(
                    queue_name=queue_name,
                    depth=depth,
                    consumer_count=consumer_count
                )

            except Exception as e:
                log.error("获取队列指标失败",
                         queue_name=queue_name,
                         error=str(e))

        return metrics

    async def _get_queue_consumer_count(self, queue_name: str) -> int:
        """获取队列消费者数量"""
        try:
            # 这里需要根据实际的Dragonfly客户端API实现
            # 暂时返回0，实际实现时需要调用相应的API
            return 0
        except Exception as e:
            log.warning("无法获取队列消费者数量",
                       queue_name=queue_name,
                       error=str(e))
            return 0

    async def _make_scaling_decision(
        self,
        deployment_name: str,
        config: DeploymentConfig,
        queue_metrics: Dict[str, QueueMetrics]
    ) -> Optional[ScalingDecision]:
        """制定扩缩容决策"""

        # 获取当前副本数
        current_replicas = await self._get_current_replicas(deployment_name)
        if current_replicas is None:
            return None

        # 获取相关队列的指标
        related_queues = [q for q, d in self.queue_deployment_mapping.items()
                         if d == deployment_name]

        if not related_queues:
            return None

        # 计算总队列深度
        total_depth = sum(queue_metrics.get(q, QueueMetrics(q, 0, 0)).depth
                         for q in related_queues)

        # 选择主要队列用于决策
        primary_queue = related_queues[0]  # 简化处理，选择第一个队列
        primary_metrics = queue_metrics.get(primary_queue,
                                           QueueMetrics(primary_queue, 0, 0))

        # 扩缩容逻辑
        target_replicas = current_replicas
        action = ScalingAction.NO_ACTION
        reason = "queue_depth_within_normal_range"

        if total_depth >= config.scale_up_threshold:
            # 需要扩容
            target_replicas = min(config.max_replicas,
                                 current_replicas + self._calculate_scale_up_amount(total_depth, config))
            action = ScalingAction.SCALE_UP
            reason = f"queue_depth_high ({total_depth} >= {config.scale_up_threshold})"

        elif total_depth <= config.scale_down_threshold and current_replicas > config.min_replicas:
            # 可以缩容
            target_replicas = max(config.min_replicas,
                                 current_replicas - self._calculate_scale_down_amount(total_depth, config))
            action = ScalingAction.SCALE_DOWN
            reason = f"queue_depth_low ({total_depth} <= {config.scale_down_threshold})"

        # 检查冷却时间
        if not self._can_scale_now(deployment_name, action):
            return ScalingDecision(
                deployment_name=deployment_name,
                current_replicas=current_replicas,
                target_replicas=current_replicas,
                action=ScalingAction.NO_ACTION,
                reason="in_cooldown_period",
                queue_metrics=primary_metrics
            )

        return ScalingDecision(
            deployment_name=deployment_name,
            current_replicas=current_replicas,
            target_replicas=target_replicas,
            action=action,
            reason=reason,
            queue_metrics=primary_metrics
        )

    def _calculate_scale_up_amount(self, queue_depth: int, config: DeploymentConfig) -> int:
        """计算扩容数量"""
        # 简单策略：每50个任务增加1个副本
        additional_replicas = max(1, queue_depth // 50)
        return min(additional_replicas, 3)  # 最多一次增加3个

    def _calculate_scale_down_amount(self, queue_depth: int, config: DeploymentConfig) -> int:
        """计算缩容数量"""
        # 保守策略：一次只减少1个副本
        return 1

    def _can_scale_now(self, deployment_name: str, action: ScalingAction) -> bool:
        """检查是否可以执行扩缩容操作（冷却时间检查）"""
        if action == ScalingAction.NO_ACTION:
            return True

        last_action_time = self.last_scaling_actions.get(deployment_name)
        if not last_action_time:
            return True

        time_since_last_action = datetime.now() - last_action_time
        return time_since_last_action > self.scaling_cooldown

    async def _get_current_replicas(self, deployment_name: str) -> Optional[int]:
        """获取当前副本数"""
        try:
            deployment = self.k8s_client.read_namespaced_deployment(
                name=deployment_name,
                namespace=self.namespace
            )
            return deployment.spec.replicas

        except ApiException as e:
            log.error("获取部署副本数失败",
                     deployment=deployment_name,
                     error=str(e))
            return None

    async def _execute_scaling_decision(self, decision: ScalingDecision):
        """执行扩缩容决策"""
        if decision.current_replicas == decision.target_replicas:
            return

        log.info("执行扩缩容操作",
                deployment=decision.deployment_name,
                current_replicas=decision.current_replicas,
                target_replicas=decision.target_replicas,
                action=decision.action.value,
                reason=decision.reason,
                queue_depth=decision.queue_metrics.depth)

        try:
            # 获取当前部署
            deployment = self.k8s_client.read_namespaced_deployment(
                name=decision.deployment_name,
                namespace=self.namespace
            )

            # 更新副本数
            deployment.spec.replicas = decision.target_replicas

            # 应用更新
            self.k8s_client.patch_namespaced_deployment(
                name=decision.deployment_name,
                namespace=self.namespace,
                body=deployment
            )

            # 记录最后一次扩缩容时间
            self.last_scaling_actions[decision.deployment_name] = datetime.now()

            log.info("扩缩容操作执行成功",
                    deployment=decision.deployment_name,
                    new_replicas=decision.target_replicas)

        except ApiException as e:
            log.error("扩缩容操作执行失败",
                     deployment=decision.deployment_name,
                     error=str(e))

    async def manual_scale(
        self,
        deployment_name: str,
        replicas: int,
        reason: str = "manual_scale"
    ) -> bool:
        """手动扩缩容"""
        if deployment_name not in self.deployment_configs:
            log.error("无效的部署名称", deployment=deployment_name)
            return False

        config = self.deployment_configs[deployment_name]

        if replicas < config.min_replicas or replicas > config.max_replicas:
            log.error("副本数超出允许范围",
                     deployment=deployment_name,
                     replicas=replicas,
                     min_replicas=config.min_replicas,
                     max_replicas=config.max_replicas)
            return False

        try:
            current_replicas = await self._get_current_replicas(deployment_name)

            decision = ScalingDecision(
                deployment_name=deployment_name,
                current_replicas=current_replicas or 0,
                target_replicas=replicas,
                action=ScalingAction.SCALE_UP if replicas > (current_replicas or 0) else ScalingAction.SCALE_DOWN,
                reason=reason,
                queue_metrics=QueueMetrics("manual", 0, 0)
            )

            await self._execute_scaling_decision(decision)
            return True

        except Exception as e:
            log.error("手动扩缩容失败",
                     deployment=deployment_name,
                     replicas=replicas,
                     error=str(e))
            return False

    async def get_scaling_status(self) -> Dict[str, Dict]:
        """获取扩缩容状态"""
        status = {}

        for deployment_name, config in self.deployment_configs.items():
            try:
                current_replicas = await self._get_current_replicas(deployment_name)
                last_action_time = self.last_scaling_actions.get(deployment_name)

                status[deployment_name] = {
                    "current_replicas": current_replicas,
                    "min_replicas": config.min_replicas,
                    "max_replicas": config.max_replicas,
                    "last_scaling_time": last_action_time.isoformat() if last_action_time else None,
                    "can_scale": self._can_scale_now(deployment_name, ScalingAction.SCALE_UP)
                }

            except Exception as e:
                status[deployment_name] = {
                    "error": str(e),
                    "available": False
                }

        return status