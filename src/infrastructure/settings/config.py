"""
Crawler Service Settings
爬虫服务配置管理
"""

import os
from typing import List, Optional
from pydantic import Field
from pydantic_settings import BaseSettings


class CrawlerSettings(BaseSettings):
    """爬虫服务配置"""

    # 服务基础配置
    service_name: str = "saturn-mousehunter-crawler-service"
    service_port: int = Field(default=8006, env="CRAWLER_SERVICE_PORT")
    debug: bool = Field(default=False, env="DEBUG")
    log_level: str = Field(default="INFO", env="LOG_LEVEL")

    # Dragonfly队列配置
    dragonfly_host: str = Field(default="192.168.8.188", env="DRAGONFLY_HOST")
    dragonfly_port: int = Field(default=30010, env="DRAGONFLY_PORT")
    dragonfly_password: Optional[str] = Field(default=None, env="DRAGONFLY_PASSWORD")
    dragonfly_db: int = Field(default=0, env="DRAGONFLY_DB")

    # 工作器配置
    worker_id: str = Field(default="crawler-worker-01", env="WORKER_ID")
    max_concurrent_tasks: int = Field(default=5, env="MAX_CONCURRENT_TASKS")
    task_timeout_seconds: int = Field(default=300, env="TASK_TIMEOUT_SECONDS")

    # 支持的任务类型和市场
    supported_task_types: List[str] = Field(
        default=["1m_realtime", "5m_realtime", "15m_realtime", "15m_backfill", "1d_backfill"]
    )
    supported_markets: List[str] = Field(
        default=["CN", "US", "HK"]
    )

    # 代理池服务配置
    proxy_pool_host: str = Field(default="192.168.8.168", env="PROXY_POOL_HOST")
    proxy_pool_port: int = Field(default=8005, env="PROXY_POOL_PORT")
    proxy_pool_service_url: str = Field(
        default="http://192.168.8.168:8005/api/v1",
        env="PROXY_POOL_SERVICE_URL"
    )

    # Cookie和代理注入配置
    enable_proxy_injection: bool = Field(default=True, env="ENABLE_PROXY_INJECTION")
    enable_cookie_injection: bool = Field(default=True, env="ENABLE_COOKIE_INJECTION")
    proxy_rotation_enabled: bool = Field(default=True, env="PROXY_ROTATION_ENABLED")
    cookie_refresh_interval_minutes: int = Field(default=30, env="COOKIE_REFRESH_INTERVAL_MINUTES")

    # 资源质量配置
    proxy_quality_threshold: float = Field(default=0.8, env="PROXY_QUALITY_THRESHOLD")
    cookie_success_rate_threshold: float = Field(default=0.9, env="COOKIE_SUCCESS_RATE_THRESHOLD")
    resource_cache_ttl_minutes: int = Field(default=60, env="RESOURCE_CACHE_TTL_MINUTES")

    # HTTP客户端配置
    http_timeout_seconds: int = Field(default=30, env="HTTP_TIMEOUT_SECONDS")
    http_max_retries: int = Field(default=3, env="HTTP_MAX_RETRIES")
    http_retry_delay_seconds: float = Field(default=1.0, env="HTTP_RETRY_DELAY_SECONDS")

    # 用户代理配置
    default_user_agents: List[str] = Field(
        default=[
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        ]
    )

    # 监控和指标配置
    metrics_enabled: bool = Field(default=True, env="METRICS_ENABLED")
    health_check_interval_seconds: int = Field(default=30, env="HEALTH_CHECK_INTERVAL_SECONDS")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


# 全局设置实例
settings = CrawlerSettings()