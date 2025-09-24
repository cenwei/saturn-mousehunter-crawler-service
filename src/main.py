"""
Saturn MouseHunter Crawler Service
爬虫服务主入口 - Dragonfly任务消费和Web数据抓取
"""

import asyncio
import signal
import sys
from contextlib import asynccontextmanager
from typing import Dict, Any

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from saturn_mousehunter_shared.log.logger import get_logger
from infrastructure.settings.config import settings
from application.consumer.dragonfly_task_consumer import CrawlerTaskConsumer
from application.services.proxy_integration_service import ProxyIntegrationService
from application.services.crawler_engine import CrawlerEngine
from interfaces.api.health import router as health_router
from interfaces.api.crawler_management import router as crawler_router

log = get_logger(__name__)

# 全局组件实例
crawler_consumer: CrawlerTaskConsumer = None
proxy_service: ProxyIntegrationService = None
crawler_engine: CrawlerEngine = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global crawler_consumer, proxy_service, crawler_engine

    log.info("启动Saturn MouseHunter爬虫服务",
            service_name=settings.service_name,
            service_port=settings.service_port,
            worker_id=settings.worker_id)

    # 初始化组件
    try:
        # 初始化代理服务
        proxy_service = ProxyIntegrationService()
        await proxy_service.initialize()

        # 初始化爬虫引擎
        crawler_engine = CrawlerEngine(proxy_service=proxy_service)
        await crawler_engine.initialize()

        # 初始化任务消费者
        crawler_consumer = CrawlerTaskConsumer(
            crawler_engine=crawler_engine,
            worker_id=settings.worker_id,
            max_concurrent_tasks=settings.max_concurrent_tasks,
            task_timeout_seconds=settings.task_timeout_seconds,
            supported_task_types=settings.supported_task_types,
            supported_markets=settings.supported_markets
        )
        await crawler_consumer.initialize()

        # 启动任务消费者
        await crawler_consumer.start()

        log.info("爬虫服务组件启动完成")

    except Exception as e:
        log.error("爬虫服务启动失败", error=str(e))
        raise

    yield

    # 关闭组件
    log.info("关闭爬虫服务组件")

    try:
        if crawler_consumer:
            await crawler_consumer.stop()

        if crawler_engine:
            await crawler_engine.shutdown()

        if proxy_service:
            await proxy_service.close()

        log.info("爬虫服务组件关闭完成")

    except Exception as e:
        log.error("爬虫服务关闭失败", error=str(e))


def create_app() -> FastAPI:
    """创建FastAPI应用"""

    app = FastAPI(
        title="Saturn MouseHunter Crawler Service",
        description="爬虫服务 - Dragonfly任务消费和Web数据抓取",
        version="0.1.0",
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
        lifespan=lifespan
    )

    # CORS中间件
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.debug else ["http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 路由注册
    app.include_router(health_router, prefix="/health", tags=["健康检查"])
    app.include_router(crawler_router, prefix="/api/v1/crawler", tags=["爬虫管理"])

    @app.get("/")
    async def root():
        """根路径"""
        return {
            "service": "saturn-mousehunter-crawler-service",
            "version": "0.1.0",
            "status": "running",
            "worker_id": settings.worker_id,
            "supported_task_types": settings.supported_task_types,
            "supported_markets": settings.supported_markets
        }

    return app


def setup_signal_handlers():
    """设置信号处理器"""

    def signal_handler(signum, frame):
        log.info("收到终止信号", signal=signum)
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


def main():
    """主函数"""
    setup_signal_handlers()

    app = create_app()

    # 启动服务
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.service_port,
        log_level=settings.log_level.lower(),
        access_log=settings.debug,
        reload=settings.debug
    )


if __name__ == "__main__":
    main()