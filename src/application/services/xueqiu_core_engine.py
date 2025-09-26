"""
CoreCrawler 雪球数据爬取引擎
基于 Dragonfly Stream 的雪球API数据抓取器

参考之前的雪球爬虫方案，对齐其接口设计：
1) 从 Dragonfly/Redis 的任务流（XREADGROUP）拉取爬虫任务
2) 为每个任务注入 Cookie（必需）与 Proxy（可选）
3) 无代理时全局并发限制为 5；有代理时可放宽
4) 每个任务可设定超时，但最大不超过 45 秒
5) 成功/失败均写入结果队列，供下游处理
"""
import asyncio
import json
import time
import random
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime
from urllib.parse import urlencode

import httpx
from saturn_mousehunter_shared.log.logger import get_logger
from saturn_mousehunter_shared.mq.dragonfly_client import DragonflyClient
from saturn_mousehunter_shared.mq.message_types import DragonflyTask, QueuePriority

from infrastructure.settings.config import CrawlerSettings

log = get_logger(__name__)


class XueqiuCoreEngine:
    """
    雪球核心爬虫引擎
    参考 CoreCrawler 设计，专门优化雪球API调用
    """

    def __init__(self, settings: CrawlerSettings):
        self.settings = settings
        self.dragonfly_client = None

        # 雪球API端点配置
        self.xueqiu_endpoints = {
            # K线数据
            "kline": "https://stock.xueqiu.com/v5/stock/chart/kline.json",
            # 实时行情
            "quote": "https://stock.xueqiu.com/v5/stock/quote.json",
            # 批量行情
            "batch_quote": "https://stock.xueqiu.com/v5/stock/batch/quote.json",
            # 分时数据
            "minute": "https://stock.xueqiu.com/v5/stock/chart/minute.json",
            # 股票基本信息
            "detail": "https://stock.xueqiu.com/v5/stock/f10/cn/company.json"
        }

        # 时间周期映射（雪球格式）
        self.period_mapping = {
            "1m": "1m",
            "5m": "5m",
            "15m": "15m",
            "30m": "30m",
            "1h": "60m",
            "1d": "day",
            "1w": "week",
            "1M": "month"
        }

        # 并发控制
        self.sem_no_proxy = asyncio.BoundedSemaphore(settings.max_concurrent_tasks or 5)
        self.sem_with_proxy = asyncio.BoundedSemaphore(20)  # 有代理时可以更多并发

    async def initialize(self):
        """初始化爬虫引擎"""
        try:
            # 初始化 Dragonfly 客户端
            self.dragonfly_client = DragonflyClient(
                service_name="saturn-crawler",
                host=self.settings.dragonfly_host,
                port=self.settings.dragonfly_port,
                password=self.settings.dragonfly_password,
                db=self.settings.dragonfly_db
            )

            await self.dragonfly_client.connect()

            log.info("雪球核心爬虫引擎初始化成功",
                    supported_endpoints=list(self.xueqiu_endpoints.keys()),
                    max_concurrent=self.settings.max_concurrent_tasks)

        except Exception as e:
            log.error("雪球核心爬虫引擎初始化失败", error=str(e))
            raise

    async def execute_task_with_streams(self, task_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行单个爬取任务 (Stream模式)
        参考 CoreCrawler 的 _handle_one 方法

        任务字段约定:
        - url: 可选，如果不提供则根据 endpoint + 参数构建
        - endpoint: 雪球API端点名 (kline/quote/batch_quote/minute/detail)
        - method: GET/POST (默认 GET)
        - symbol: 股票代码 (必填)
        - params: API参数字典
        - headers: 可选请求头
        - cookie_id: Cookie标识符 (必需)
        - proxy: 指定代理 (可选)
        - timeout: 超时时间，秒 (默认30，上限45)
        """
        task_id = task_data.get("task_id", f"task_{int(time.time())}")
        start_time = time.time()

        try:
            # 提取任务参数
            endpoint = task_data.get("endpoint", "kline")
            symbol = task_data.get("symbol")
            method = (task_data.get("method") or "GET").upper()
            params = task_data.get("params", {})
            headers = task_data.get("headers", {})
            cookie_id = task_data.get("cookie_id", "")
            proxy = task_data.get("proxy")
            timeout = self._clamp_timeout(task_data.get("timeout"))

            if not symbol:
                return await self._create_fail_result(task_id, task_data, "missing_symbol")

            # 获取Cookie (必需)
            cookie_text = await self._get_cookie(cookie_id)
            if not cookie_text:
                return await self._create_fail_result(task_id, task_data, "missing_cookie")

            # 构建请求
            url, final_headers = await self._build_xueqiu_request(
                endpoint, symbol, params, headers, cookie_text
            )

            # 获取代理 (可选)
            if not proxy:
                proxy = await self._get_random_proxy()

            # 并发控制
            semaphore = self.sem_with_proxy if proxy else self.sem_no_proxy

            async with semaphore:
                # 执行HTTP请求
                result = await asyncio.wait_for(
                    self._do_xueqiu_fetch(url, method, final_headers, params, proxy, timeout),
                    timeout=timeout
                )

                # 处理结果
                if result["success"]:
                    duration = time.time() - start_time
                    log.info("雪球任务执行成功",
                            task_id=task_id,
                            endpoint=endpoint,
                            symbol=symbol,
                            duration=f"{duration:.2f}s",
                            records=result.get("records_count", 0))

                    return await self._create_success_result(task_id, result, duration)
                else:
                    return await self._create_fail_result(task_id, task_data, result["error"])

        except asyncio.TimeoutError:
            return await self._create_fail_result(task_id, task_data, "task_timeout")
        except Exception as e:
            log.error("雪球任务执行异常", task_id=task_id, error=str(e))
            return await self._create_fail_result(task_id, task_data, str(e))

    async def _build_xueqiu_request(
        self,
        endpoint: str,
        symbol: str,
        params: Dict[str, Any],
        headers: Dict[str, str],
        cookie_text: str
    ) -> Tuple[str, Dict[str, str]]:
        """构建雪球API请求"""

        # 获取API端点URL
        base_url = self.xueqiu_endpoints.get(endpoint, self.xueqiu_endpoints["kline"])

        # 设置雪球标准请求头
        final_headers = {
            "User-Agent": self._get_random_user_agent(),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": f"https://xueqiu.com/S/{symbol}",
            "Origin": "https://xueqiu.com",
            "X-Requested-With": "XMLHttpRequest",
            **headers  # 合并用户自定义头
        }

        # 设置Cookie
        if "Cookie" in final_headers:
            if cookie_text not in final_headers["Cookie"]:
                final_headers["Cookie"] = f"{final_headers['Cookie']}; {cookie_text}"
        else:
            final_headers["Cookie"] = cookie_text

        # 构建完整URL (GET请求参数会自动处理)
        return base_url, final_headers

    async def _do_xueqiu_fetch(
        self,
        url: str,
        method: str,
        headers: Dict[str, str],
        params: Dict[str, Any],
        proxy: Optional[str],
        timeout: float
    ) -> Dict[str, Any]:
        """执行雪球API抓取"""

        # 配置HTTP超时
        http_timeout = httpx.Timeout(
            connect=self.settings.http_timeout_seconds,
            read=min(self.settings.http_timeout_seconds, timeout),
            write=self.settings.http_timeout_seconds,
            pool=None
        )

        # 配置代理
        proxies = {"http://": proxy, "https://": proxy} if proxy else None

        try:
            async with httpx.AsyncClient(
                timeout=http_timeout,
                proxies=proxies,
                follow_redirects=True,
                headers={"Connection": "keep-alive"}
            ) as client:

                if method == "GET":
                    response = await client.get(url, params=params, headers=headers)
                elif method == "POST":
                    response = await client.post(url, json=params, headers=headers)
                else:
                    raise ValueError(f"不支持的HTTP方法: {method}")

                # 处理响应
                if response.status_code == 200:
                    try:
                        data = response.json()

                        # 雪球API标准响应格式检查
                        if data.get("error_code") == 0:
                            return {
                                "success": True,
                                "data": data.get("data", {}),
                                "status_code": response.status_code,
                                "response_time": response.elapsed.total_seconds(),
                                "records_count": self._count_records(data),
                                "proxy_used": proxy or "",
                                "raw_response": data
                            }
                        else:
                            error_msg = data.get("error_description", f"API错误码: {data.get('error_code')}")
                            return {
                                "success": False,
                                "error": f"xueqiu_api_error: {error_msg}",
                                "status_code": response.status_code,
                                "raw_response": data
                            }
                    except json.JSONDecodeError as e:
                        return {
                            "success": False,
                            "error": f"json_decode_error: {str(e)}",
                            "status_code": response.status_code,
                            "raw_text": response.text[:1000]  # 截断长文本
                        }
                else:
                    return {
                        "success": False,
                        "error": f"http_error: {response.status_code}",
                        "status_code": response.status_code,
                        "response_text": response.text[:500]
                    }

        except httpx.ConnectTimeout:
            return {"success": False, "error": "connect_timeout"}
        except httpx.ReadTimeout:
            return {"success": False, "error": "read_timeout"}
        except Exception as e:
            return {"success": False, "error": f"request_error: {str(e)}"}

    def _count_records(self, data: Dict[str, Any]) -> int:
        """计算返回的记录数量"""
        try:
            if "data" in data:
                api_data = data["data"]

                # K线数据
                if "item" in api_data and isinstance(api_data["item"], list):
                    return len(api_data["item"])

                # 行情数据
                if "list" in api_data and isinstance(api_data["list"], list):
                    return len(api_data["list"])

                # 分时数据
                if "items" in api_data and isinstance(api_data["items"], list):
                    return len(api_data["items"])

                # 单个记录
                if isinstance(api_data, dict) and api_data:
                    return 1

            return 0
        except:
            return 0

    async def _get_cookie(self, cookie_id: str) -> Optional[str]:
        """从Redis获取Cookie"""
        if not cookie_id or not self.dragonfly_client:
            return None

        try:
            # 使用 Dragonfly 客户端获取缓存的Cookie
            cookie_data = await self.dragonfly_client.get_cached_resource(
                "cookie", "CN", cookie_id
            )
            return cookie_data.get("cookie_text") if cookie_data else None
        except Exception as e:
            log.warning("获取Cookie失败", cookie_id=cookie_id, error=str(e))
            return None

    async def _get_random_proxy(self) -> Optional[str]:
        """获取随机代理"""
        try:
            if not self.dragonfly_client:
                return None

            # 获取可用代理列表
            proxy_list = await self.dragonfly_client.get_cached_resource(
                "proxy", "CN", "active_proxies"
            )

            if proxy_list and isinstance(proxy_list.get("proxies"), list):
                proxies = proxy_list["proxies"]
                return random.choice(proxies) if proxies else None

            return None
        except Exception as e:
            log.warning("获取代理失败", error=str(e))
            return None

    def _get_random_user_agent(self) -> str:
        """获取随机User-Agent"""
        agents = self.settings.default_user_agents
        return random.choice(agents) if agents else \
               "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"

    async def _create_success_result(self, task_id: str, result: Dict[str, Any], duration: float) -> Dict[str, Any]:
        """创建成功结果"""
        return {
            "task_id": task_id,
            "success": True,
            "data": result.get("data", {}),
            "status_code": result.get("status_code", 200),
            "response_time": duration,
            "records_count": result.get("records_count", 0),
            "proxy_used": result.get("proxy_used", ""),
            "timestamp": int(time.time()),
            "metadata": {
                "engine": "xueqiu_core",
                "version": "1.0.0"
            }
        }

    async def _create_fail_result(self, task_id: str, task_data: Dict[str, Any], reason: str) -> Dict[str, Any]:
        """创建失败结果"""
        return {
            "task_id": task_id,
            "success": False,
            "error": reason,
            "timestamp": int(time.time()),
            "task_data": task_data,
            "metadata": {
                "engine": "xueqiu_core",
                "version": "1.0.0"
            }
        }

    @staticmethod
    def _clamp_timeout(timeout_value: Any, default: float = 30.0, max_cap: float = 45.0) -> float:
        """限制超时时间"""
        try:
            timeout = float(timeout_value) if timeout_value is not None else default
        except (ValueError, TypeError):
            timeout = default
        return max(5.0, min(timeout, max_cap))

    # ============= 雪球API专用方法 =============

    async def fetch_kline_data(
        self,
        symbol: str,
        period: str = "1d",
        count: int = 100,
        cookie_id: str = "",
        proxy: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        获取K线数据

        Args:
            symbol: 股票代码 (如 SH600000)
            period: 时间周期 (1m/5m/15m/30m/1h/1d/1w/1M)
            count: 数据条数 (-100表示最近100条)
            cookie_id: Cookie标识符
            proxy: 指定代理
        """
        current_time = int(time.time() * 1000)

        task_data = {
            "task_id": f"kline_{symbol}_{period}_{int(time.time())}",
            "endpoint": "kline",
            "symbol": symbol,
            "cookie_id": cookie_id,
            "proxy": proxy,
            "params": {
                "symbol": symbol,
                "begin": current_time,
                "period": self.period_mapping.get(period, "day"),
                "type": "before",
                "count": -abs(count),  # 负数表示最新的N条
                "indicator": "kline,pe,pb,ps,pcf,market_capital,agt,ggt,balance"
            }
        }

        return await self.execute_task_with_streams(task_data)

    async def fetch_realtime_quote(
        self,
        symbol: str,
        cookie_id: str = "",
        proxy: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        获取实时行情

        Args:
            symbol: 股票代码
            cookie_id: Cookie标识符
            proxy: 指定代理
        """
        task_data = {
            "task_id": f"quote_{symbol}_{int(time.time())}",
            "endpoint": "quote",
            "symbol": symbol,
            "cookie_id": cookie_id,
            "proxy": proxy,
            "params": {
                "symbol": symbol,
                "extend": "detail"
            }
        }

        return await self.execute_task_with_streams(task_data)

    async def fetch_batch_quotes(
        self,
        symbols: List[str],
        cookie_id: str = "",
        proxy: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        批量获取行情

        Args:
            symbols: 股票代码列表
            cookie_id: Cookie标识符
            proxy: 指定代理
        """
        symbol_str = ",".join(symbols)

        task_data = {
            "task_id": f"batch_quote_{len(symbols)}_{int(time.time())}",
            "endpoint": "batch_quote",
            "symbol": symbol_str,  # 用作标识
            "cookie_id": cookie_id,
            "proxy": proxy,
            "params": {
                "symbol": symbol_str,
                "extend": "detail"
            }
        }

        return await self.execute_task_with_streams(task_data)

    async def fetch_minute_data(
        self,
        symbol: str,
        cookie_id: str = "",
        proxy: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        获取分时数据

        Args:
            symbol: 股票代码
            cookie_id: Cookie标识符
            proxy: 指定代理
        """
        task_data = {
            "task_id": f"minute_{symbol}_{int(time.time())}",
            "endpoint": "minute",
            "symbol": symbol,
            "cookie_id": cookie_id,
            "proxy": proxy,
            "params": {
                "symbol": symbol,
                "period": "1d"
            }
        }

        return await self.execute_task_with_streams(task_data)

    async def shutdown(self):
        """关闭引擎"""
        try:
            if self.dragonfly_client:
                await self.dragonfly_client.disconnect()
            log.info("雪球核心爬虫引擎已关闭")
        except Exception as e:
            log.error("关闭引擎失败", error=str(e))