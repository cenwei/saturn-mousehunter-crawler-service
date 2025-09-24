"""
健康检查API
爬虫服务健康状态检查接口
"""
from typing import Dict, Any
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from saturn_mousehunter_shared.log.logger import get_logger

log = get_logger(__name__)

router = APIRouter()


class HealthStatus(BaseModel):
    """健康状态模型"""
    status: str
    timestamp: str
    service: str
    version: str
    uptime_seconds: float


class ReadinessStatus(BaseModel):
    """就绪状态模型"""
    ready: bool
    components: Dict[str, bool]
    timestamp: str


# 服务启动时间
service_start_time = datetime.now()


@router.get("/status", response_model=HealthStatus)
async def get_health_status():
    """获取服务健康状态"""
    try:
        uptime = (datetime.now() - service_start_time).total_seconds()

        return HealthStatus(
            status="healthy",
            timestamp=datetime.now().isoformat(),
            service="saturn-mousehunter-crawler-service",
            version="0.1.0",
            uptime_seconds=uptime
        )

    except Exception as e:
        log.error("健康检查失败", error=str(e))
        raise HTTPException(status_code=500, detail="健康检查失败")


@router.get("/ready", response_model=ReadinessStatus)
async def get_readiness_status():
    """获取服务就绪状态"""
    try:
        # 检查组件状态
        components_status = {
            "dragonfly_consumer": True,  # 简化版本，实际应检查消费者状态
            "proxy_service": True,       # 实际应检查代理服务状态
            "crawler_engine": True       # 实际应检查爬虫引擎状态
        }

        all_ready = all(components_status.values())

        return ReadinessStatus(
            ready=all_ready,
            components=components_status,
            timestamp=datetime.now().isoformat()
        )

    except Exception as e:
        log.error("就绪检查失败", error=str(e))
        raise HTTPException(status_code=500, detail="就绪检查失败")


@router.get("/ping")
async def ping():
    """简单的ping检查"""
    return {"message": "pong", "timestamp": datetime.now().isoformat()}