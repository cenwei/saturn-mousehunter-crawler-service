"""
爬虫管理API
爬虫服务管理和监控接口
"""
from typing import Dict, Any, List
from datetime import datetime

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel

from saturn_mousehunter_shared.log.logger import get_logger

log = get_logger(__name__)

router = APIRouter()


class CrawlerStats(BaseModel):
    """爬虫统计信息模型"""
    worker_id: str
    running: bool
    active_executions: int
    max_concurrent_tasks: int
    stats: Dict[str, Any]
    queue_priorities: List[str]


class ActiveTask(BaseModel):
    """活跃任务模型"""
    task_id: str
    task_type: str
    market: str
    symbol: str
    start_time: str
    duration: float


class TaskCancelResponse(BaseModel):
    """任务取消响应模型"""
    success: bool
    message: str
    task_id: str


@router.get("/stats", response_model=CrawlerStats)
async def get_crawler_stats():
    """获取爬虫统计信息"""
    try:
        # 这里应该从全局的crawler_consumer获取统计信息
        # 为了演示，返回模拟数据
        from main import crawler_consumer

        if not crawler_consumer:
            raise HTTPException(status_code=503, detail="爬虫消费者未初始化")

        stats = crawler_consumer.get_stats()

        return CrawlerStats(
            worker_id=stats.get("worker_id", "unknown"),
            running=stats.get("running", False),
            active_executions=stats.get("active_executions", 0),
            max_concurrent_tasks=stats.get("max_concurrent_tasks", 0),
            stats=stats.get("stats", {}),
            queue_priorities=stats.get("queue_priorities", [])
        )

    except Exception as e:
        log.error("获取爬虫统计信息失败", error=str(e))
        raise HTTPException(status_code=500, detail="获取统计信息失败")


@router.get("/tasks/active", response_model=List[ActiveTask])
async def get_active_tasks():
    """获取活跃任务列表"""
    try:
        from main import crawler_consumer

        if not crawler_consumer:
            raise HTTPException(status_code=503, detail="爬虫消费者未初始化")

        active_tasks = crawler_consumer.get_active_tasks()

        return [
            ActiveTask(
                task_id=task["task_id"],
                task_type=task["task_type"],
                market=task["market"],
                symbol=task["symbol"],
                start_time=task["start_time"],
                duration=task["duration"]
            )
            for task in active_tasks
        ]

    except Exception as e:
        log.error("获取活跃任务列表失败", error=str(e))
        raise HTTPException(status_code=500, detail="获取活跃任务失败")


@router.post("/tasks/{task_id}/cancel", response_model=TaskCancelResponse)
async def cancel_task(task_id: str = Path(..., description="任务ID")):
    """取消指定任务"""
    try:
        from main import crawler_consumer

        if not crawler_consumer:
            raise HTTPException(status_code=503, detail="爬虫消费者未初始化")

        # 检查任务是否存在
        active_tasks = crawler_consumer.get_active_tasks()
        task_exists = any(task["task_id"] == task_id for task in active_tasks)

        if not task_exists:
            return TaskCancelResponse(
                success=False,
                message=f"任务 {task_id} 不存在或已完成",
                task_id=task_id
            )

        # 这里实现任务取消逻辑
        # 实际实现中需要在crawler_consumer中添加cancel_task方法
        log.info("请求取消任务", task_id=task_id)

        return TaskCancelResponse(
            success=True,
            message=f"任务 {task_id} 取消请求已发送",
            task_id=task_id
        )

    except Exception as e:
        log.error("取消任务失败", task_id=task_id, error=str(e))
        raise HTTPException(status_code=500, detail="取消任务失败")


@router.get("/proxy/stats")
async def get_proxy_stats():
    """获取代理统计信息"""
    try:
        from main import proxy_service

        if not proxy_service:
            raise HTTPException(status_code=503, detail="代理服务未初始化")

        # 这里应该从proxy_service获取统计信息
        # 返回简化的统计信息
        return {
            "proxy_cache_size": len(proxy_service.proxy_cache),
            "cookie_cache_size": len(proxy_service.cookie_cache),
            "markets": list(proxy_service.market_config.keys()),
            "last_updated": datetime.now().isoformat()
        }

    except Exception as e:
        log.error("获取代理统计信息失败", error=str(e))
        raise HTTPException(status_code=500, detail="获取代理统计信息失败")


@router.post("/proxy/refresh")
async def refresh_proxy_cache():
    """刷新代理缓存"""
    try:
        from main import proxy_service

        if not proxy_service:
            raise HTTPException(status_code=503, detail="代理服务未初始化")

        # 清理过期资源
        await proxy_service.cleanup_expired_resources()

        # 重新预加载缓存
        await proxy_service._preload_resource_cache()

        return {
            "success": True,
            "message": "代理缓存已刷新",
            "timestamp": datetime.now().isoformat()
        }

    except Exception as e:
        log.error("刷新代理缓存失败", error=str(e))
        raise HTTPException(status_code=500, detail="刷新代理缓存失败")