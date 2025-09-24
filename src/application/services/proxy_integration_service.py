"""
代理集成服务
负责根据任务类型自动注入代理和Cookie资源
"""
import asyncio
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum

import httpx
from saturn_mousehunter_shared.log.logger import get_logger
from saturn_mousehunter_shared.mq.dragonfly_client import DragonflyClient
from saturn_mousehunter_shared.mq.message_types import DragonflyTask

from infrastructure.settings.config import CrawlerSettings

log = get_logger(__name__)


class ResourceType(Enum):
    """资源类型枚举"""
    COOKIE = "COOKIE"
    PROXY = "PROXY"


class TaskTypeResourceMapping(Enum):
    """任务类型资源映射"""
    # 实时任务需要高质量代理和新鲜Cookie
    REALTIME_1M = ("1m_realtime", True, True, "HIGH")
    REALTIME_5M = ("5m_realtime", True, True, "HIGH")
    REALTIME_15M = ("15m_realtime", True, True, "MEDIUM")

    # 回填任务可以使用普通代理和缓存Cookie
    BACKFILL_15M = ("15m_backfill", True, False, "MEDIUM")
    BACKFILL_1D = ("1d_backfill", True, False, "LOW")


@dataclass
class ProxyResource:
    """代理资源"""
    proxy_id: str
    proxy_url: str
    username: Optional[str] = None
    password: Optional[str] = None
    market: str = "CN"
    quality_score: float = 1.0
    last_used: Optional[datetime] = None
    success_rate: float = 1.0
    avg_response_time: float = 0.0


@dataclass
class CookieResource:
    """Cookie资源"""
    cookie_id: str
    cookies: Dict[str, str]
    market: str = "CN"
    expires_at: datetime = None
    domain: str = ""
    success_rate: float = 1.0
    last_validated: Optional[datetime] = None


@dataclass
class InjectionContext:
    """注入上下文"""
    task: DragonflyTask
    proxy: Optional[ProxyResource] = None
    cookies: Optional[CookieResource] = None
    headers: Dict[str, str] = None
    timeout: int = 30


class ProxyIntegrationService:
    """代理集成服务"""

    def __init__(self, settings: Optional[CrawlerSettings] = None):
        self.settings = settings or CrawlerSettings()
        self.dragonfly_client: Optional[DragonflyClient] = None

        # 资源缓存
        self.proxy_cache: Dict[str, List[ProxyResource]] = {}
        self.cookie_cache: Dict[str, List[CookieResource]] = {}

        # 任务类型资源配置
        self.task_resource_config = {
            "1m_realtime": {"proxy_quality": "HIGH", "cookie_fresh": True, "priority": 1},
            "5m_realtime": {"proxy_quality": "HIGH", "cookie_fresh": True, "priority": 2},
            "15m_realtime": {"proxy_quality": "MEDIUM", "cookie_fresh": True, "priority": 3},
            "15m_backfill": {"proxy_quality": "MEDIUM", "cookie_fresh": False, "priority": 4},
            "1d_backfill": {"proxy_quality": "LOW", "cookie_fresh": False, "priority": 5}
        }

        # 市场特定配置
        self.market_config = {
            "CN": {
                "proxy_pool_endpoint": f"http://{self.settings.proxy_pool_host}:{self.settings.proxy_pool_port}/api/v1/pools/xueqiu",
                "cookie_domains": ["xueqiu.com", "snowballsecurities.com"],
                "user_agents": [
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Safari/605.1.15"
                ]
            },
            "US": {
                "proxy_pool_endpoint": f"http://{self.settings.proxy_pool_host}:{self.settings.proxy_pool_port}/api/v1/pools/nasdaq",
                "cookie_domains": ["nasdaq.com", "finance.yahoo.com"],
                "user_agents": [
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                ]
            },
            "HK": {
                "proxy_pool_endpoint": f"http://{self.settings.proxy_pool_host}:{self.settings.proxy_pool_port}/api/v1/pools/hkex",
                "cookie_domains": ["hkex.com.hk", "aastocks.com"],
                "user_agents": [
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                ]
            }
        }

    async def initialize(self):
        """初始化服务"""
        try:
            # 初始化Dragonfly客户端
            self.dragonfly_client = DragonflyClient(
                service_name="saturn-mousehunter-crawler-service",
                host=self.settings.dragonfly_host,
                port=self.settings.dragonfly_port,
                db=self.settings.dragonfly_db
            )
            await self.dragonfly_client.initialize()

            # 预热资源缓存
            await self._preload_resource_cache()

            log.info("代理集成服务初始化成功",
                    proxy_pool_host=self.settings.proxy_pool_host,
                    dragonfly_host=self.settings.dragonfly_host)

        except Exception as e:
            log.error("代理集成服务初始化失败", error=str(e))
            raise

    async def prepare_injection_context(self, task: DragonflyTask) -> InjectionContext:
        """
        为任务准备注入上下文

        Args:
            task: Dragonfly任务

        Returns:
            注入上下文
        """
        try:
            # 获取任务类型配置
            task_config = self.task_resource_config.get(task.task_type, {})
            market_config = self.market_config.get(task.market, {})

            # 创建注入上下文
            context = InjectionContext(task=task)

            # 注入代理
            if self.settings.enable_proxy_injection:
                context.proxy = await self._get_proxy_for_task(task, task_config)

            # 注入Cookie
            if self.settings.enable_cookie_injection:
                context.cookies = await self._get_cookies_for_task(task, task_config)

            # 构建请求头
            context.headers = self._build_request_headers(task, market_config, context)

            # 设置超时
            context.timeout = self._get_timeout_for_task(task)

            log.info("注入上下文准备完成",
                    task_id=task.task_id,
                    task_type=task.task_type,
                    market=task.market,
                    has_proxy=context.proxy is not None,
                    has_cookies=context.cookies is not None)

            return context

        except Exception as e:
            log.error("准备注入上下文失败",
                     task_id=task.task_id,
                     error=str(e))
            raise

    async def _get_proxy_for_task(self, task: DragonflyTask, task_config: Dict) -> Optional[ProxyResource]:
        """为任务获取代理"""
        try:
            market = task.market
            quality = task_config.get("proxy_quality", "MEDIUM")

            # 先从缓存获取
            cached_proxies = self.proxy_cache.get(f"{market}:{quality}", [])
            if cached_proxies:
                # 选择最优代理（成功率高、响应时间短）
                best_proxy = max(cached_proxies,
                               key=lambda p: p.success_rate - (p.avg_response_time / 1000))
                best_proxy.last_used = datetime.now()
                return best_proxy

            # 从代理池服务获取
            proxy_data = await self._fetch_proxy_from_pool(market, quality)
            if proxy_data:
                proxy = ProxyResource(
                    proxy_id=proxy_data.get("proxy_id"),
                    proxy_url=proxy_data.get("proxy_url"),
                    username=proxy_data.get("username"),
                    password=proxy_data.get("password"),
                    market=market,
                    quality_score=proxy_data.get("quality_score", 1.0),
                    success_rate=proxy_data.get("success_rate", 1.0),
                    avg_response_time=proxy_data.get("avg_response_time", 0.0),
                    last_used=datetime.now()
                )

                # 更新缓存
                cache_key = f"{market}:{quality}"
                if cache_key not in self.proxy_cache:
                    self.proxy_cache[cache_key] = []
                self.proxy_cache[cache_key].append(proxy)

                return proxy

        except Exception as e:
            log.error("获取代理失败",
                     task_id=task.task_id,
                     market=task.market,
                     error=str(e))

        return None

    async def _get_cookies_for_task(self, task: DragonflyTask, task_config: Dict) -> Optional[CookieResource]:
        """为任务获取Cookie"""
        try:
            market = task.market
            need_fresh = task_config.get("cookie_fresh", False)

            # 从缓存中查找有效Cookie
            cached_cookies = self.cookie_cache.get(market, [])
            for cookie_res in cached_cookies:
                # 检查是否过期
                if cookie_res.expires_at and cookie_res.expires_at < datetime.now():
                    continue

                # 如果需要新鲜Cookie，检查最后验证时间
                if need_fresh and cookie_res.last_validated:
                    if datetime.now() - cookie_res.last_validated > timedelta(minutes=30):
                        continue

                return cookie_res

            # 从Dragonfly缓存获取
            cookie_data = await self.dragonfly_client.get_cached_resource(
                ResourceType.COOKIE.value, market
            )

            if cookie_data and cookie_data.get("data"):
                cookie_info = cookie_data["data"]
                cookie_res = CookieResource(
                    cookie_id=cookie_info.get("cookie_id"),
                    cookies=cookie_info.get("cookies", {}),
                    market=market,
                    expires_at=datetime.fromisoformat(cookie_info.get("expires_at")) if cookie_info.get("expires_at") else None,
                    domain=cookie_info.get("domain", ""),
                    success_rate=cookie_info.get("success_rate", 1.0),
                    last_validated=datetime.now()
                )

                # 更新缓存
                if market not in self.cookie_cache:
                    self.cookie_cache[market] = []
                self.cookie_cache[market].append(cookie_res)

                return cookie_res

        except Exception as e:
            log.error("获取Cookie失败",
                     task_id=task.task_id,
                     market=task.market,
                     error=str(e))

        return None

    def _build_request_headers(self, task: DragonflyTask, market_config: Dict, context: InjectionContext) -> Dict[str, str]:
        """构建请求头"""
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "DNT": "1",
            "Pragma": "no-cache",
            "Sec-Ch-Ua": '"Google Chrome";v="91", "Chromium";v="91", ";Not A Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin"
        }

        # 添加User-Agent
        user_agents = market_config.get("user_agents", [])
        if user_agents:
            headers["User-Agent"] = user_agents[0]  # 可以后续实现轮换

        # 添加Referer（根据市场设置）
        if task.market == "CN":
            headers["Referer"] = "https://xueqiu.com/"
        elif task.market == "US":
            headers["Referer"] = "https://finance.yahoo.com/"
        elif task.market == "HK":
            headers["Referer"] = "https://www.hkex.com.hk/"

        # 添加任务特定头
        headers["X-Task-Id"] = task.task_id
        headers["X-Task-Type"] = task.task_type
        headers["X-Market"] = task.market

        return headers

    def _get_timeout_for_task(self, task: DragonflyTask) -> int:
        """根据任务类型获取超时时间"""
        timeout_mapping = {
            "1m_realtime": 5,    # 实时任务超时短
            "5m_realtime": 10,
            "15m_realtime": 15,
            "15m_backfill": 30,  # 回填任务超时长
            "1d_backfill": 60
        }
        return timeout_mapping.get(task.task_type, 30)

    async def _fetch_proxy_from_pool(self, market: str, quality: str) -> Optional[Dict[str, Any]]:
        """从代理池服务获取代理"""
        try:
            market_config = self.market_config.get(market, {})
            endpoint = market_config.get("proxy_pool_endpoint")

            if not endpoint:
                log.warning("未配置代理池端点", market=market)
                return None

            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    f"{endpoint}/acquire",
                    params={"quality": quality.lower()}
                )

                if response.status_code == 200:
                    return response.json()
                else:
                    log.warning("获取代理失败",
                              market=market,
                              quality=quality,
                              status_code=response.status_code)

        except Exception as e:
            log.error("从代理池获取代理失败",
                     market=market,
                     quality=quality,
                     error=str(e))

        return None

    async def _preload_resource_cache(self):
        """预加载资源缓存"""
        try:
            # 为每个市场预加载一些代理和Cookie
            for market in ["CN", "US", "HK"]:
                # 预加载代理
                for quality in ["HIGH", "MEDIUM", "LOW"]:
                    proxy_data = await self._fetch_proxy_from_pool(market, quality)
                    if proxy_data:
                        cache_key = f"{market}:{quality}"
                        if cache_key not in self.proxy_cache:
                            self.proxy_cache[cache_key] = []

                # 预加载Cookie
                cookie_data = await self.dragonfly_client.get_cached_resource(
                    ResourceType.COOKIE.value, market
                )
                if cookie_data:
                    # 处理Cookie缓存
                    pass

            log.info("资源缓存预加载完成",
                    proxy_cache_keys=list(self.proxy_cache.keys()),
                    cookie_cache_keys=list(self.cookie_cache.keys()))

        except Exception as e:
            log.error("预加载资源缓存失败", error=str(e))

    async def create_http_client(self, context: InjectionContext) -> httpx.AsyncClient:
        """
        基于注入上下文创建HTTP客户端

        Args:
            context: 注入上下文

        Returns:
            配置好的HTTP客户端
        """
        try:
            # 构建客户端配置
            client_config = {
                "timeout": context.timeout,
                "headers": context.headers,
                "follow_redirects": True,
                "verify": False  # 在爬虫场景中通常禁用SSL验证
            }

            # 配置代理
            if context.proxy:
                if context.proxy.username and context.proxy.password:
                    # 带认证的代理
                    proxy_url = f"http://{context.proxy.username}:{context.proxy.password}@{context.proxy.proxy_url.replace('http://', '')}"
                else:
                    proxy_url = context.proxy.proxy_url

                client_config["proxies"] = {
                    "http://": proxy_url,
                    "https://": proxy_url
                }

            # 配置Cookie
            if context.cookies:
                client_config["cookies"] = context.cookies.cookies

            client = httpx.AsyncClient(**client_config)

            log.debug("HTTP客户端创建成功",
                     task_id=context.task.task_id,
                     has_proxy=context.proxy is not None,
                     has_cookies=context.cookies is not None,
                     timeout=context.timeout)

            return client

        except Exception as e:
            log.error("创建HTTP客户端失败",
                     task_id=context.task.task_id,
                     error=str(e))
            raise

    async def report_resource_performance(self, context: InjectionContext, success: bool, response_time: float):
        """上报资源性能指标"""
        try:
            # 更新代理性能
            if context.proxy:
                if success:
                    context.proxy.success_rate = min(1.0, context.proxy.success_rate * 0.9 + 0.1)
                else:
                    context.proxy.success_rate = max(0.0, context.proxy.success_rate * 0.9)

                context.proxy.avg_response_time = (context.proxy.avg_response_time * 0.8 + response_time * 0.2)

            # 更新Cookie性能
            if context.cookies:
                if success:
                    context.cookies.success_rate = min(1.0, context.cookies.success_rate * 0.9 + 0.1)
                    context.cookies.last_validated = datetime.now()
                else:
                    context.cookies.success_rate = max(0.0, context.cookies.success_rate * 0.9)

        except Exception as e:
            log.error("上报资源性能失败", error=str(e))

    async def cleanup_expired_resources(self):
        """清理过期资源"""
        try:
            current_time = datetime.now()

            # 清理过期Cookie
            for market, cookies in self.cookie_cache.items():
                self.cookie_cache[market] = [
                    cookie for cookie in cookies
                    if not cookie.expires_at or cookie.expires_at > current_time
                ]

            # 清理长时间未使用的代理
            for cache_key, proxies in self.proxy_cache.items():
                self.proxy_cache[cache_key] = [
                    proxy for proxy in proxies
                    if not proxy.last_used or
                    current_time - proxy.last_used < timedelta(hours=1)
                ]

            log.debug("资源清理完成")

        except Exception as e:
            log.error("清理过期资源失败", error=str(e))

    async def close(self):
        """关闭服务"""
        try:
            if self.dragonfly_client:
                await self.dragonfly_client.close()

            # 清空缓存
            self.proxy_cache.clear()
            self.cookie_cache.clear()

            log.info("代理集成服务已关闭")

        except Exception as e:
            log.error("关闭代理集成服务失败", error=str(e))