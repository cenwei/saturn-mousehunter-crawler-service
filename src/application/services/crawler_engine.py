"""
爬虫引擎
集成代理注入的HTTP数据抓取引擎
整合雪球核心爬虫引擎，对齐之前的雪球爬虫方案接口
"""
import asyncio
import json
from typing import Dict, Any, Optional, List
from datetime import datetime
from dataclasses import dataclass

import httpx
from saturn_mousehunter_shared.log.logger import get_logger
from saturn_mousehunter_shared.mq.message_types import DragonflyTask

from application.services.proxy_integration_service import ProxyIntegrationService, InjectionContext
from application.services.xueqiu_core_engine import XueqiuCoreEngine
from infrastructure.settings.config import CrawlerSettings

log = get_logger(__name__)


@dataclass
class CrawlingResult:
    """爬取结果"""
    task_id: str
    success: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    status_code: Optional[int] = None
    response_time: float = 0.0
    records_count: int = 0
    metadata: Optional[Dict[str, Any]] = None


class CrawlerEngine:
    """爬虫引擎 - 整合雪球核心引擎"""

    def __init__(self, proxy_service: ProxyIntegrationService, settings: Optional[CrawlerSettings] = None):
        self.proxy_service = proxy_service
        self.settings = settings or CrawlerSettings()

        # 初始化雪球核心引擎
        self.xueqiu_engine = XueqiuCoreEngine(self.settings)

        # 任务处理器映射 (优先使用雪球核心引擎)
        self.task_handlers = {
            "1m_realtime": self._handle_cn_realtime_with_core,
            "5m_realtime": self._handle_cn_realtime_with_core,
            "15m_realtime": self._handle_cn_realtime_with_core,
            "15m_backfill": self._handle_cn_backfill_with_core,
            "1d_backfill": self._handle_cn_backfill_with_core,
            # 其他市场保留原有逻辑
            "us_1m_realtime": self._handle_us_realtime_kline,
            "us_5m_realtime": self._handle_us_realtime_kline,
            "hk_1m_realtime": self._handle_hk_realtime_kline
        }

        # 市场端点配置 (保留用于US/HK市场)
        self.market_endpoints = {
            "US": {
                "kline_api": "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                "quote_api": "https://query1.finance.yahoo.com/v6/finance/quote",
                "batch_quote_api": "https://query1.finance.yahoo.com/v6/finance/quote"
            },
            "HK": {
                "kline_api": "https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get",
                "quote_api": "https://web.ifzq.gtimg.cn/appstock/app/hkquote/get",
                "batch_quote_api": "https://web.ifzq.gtimg.cn/appstock/app/hkquote/get"
            }
        }

    async def initialize(self):
        """初始化爬虫引擎"""
        try:
            # 初始化雪球核心引擎
            await self.xueqiu_engine.initialize()

            log.info("爬虫引擎初始化成功",
                    supported_markets=list(self.market_endpoints.keys()) + ["CN"],
                    supported_task_types=list(self.task_handlers.keys()),
                    xueqiu_core_enabled=True)

        except Exception as e:
            log.error("爬虫引擎初始化失败", error=str(e))
            raise

    async def execute_crawling_task(self, task: DragonflyTask) -> bool:
        """
        执行爬取任务

        Args:
            task: Dragonfly任务

        Returns:
            是否成功
        """
        start_time = datetime.now()

        try:
            log.info("开始执行爬取任务",
                    task_id=task.task_id,
                    task_type=task.task_type,
                    market=task.market,
                    symbol=task.symbol)

            # 准备注入上下文
            context = await self.proxy_service.prepare_injection_context(task)

            # 获取任务处理器
            handler = self.task_handlers.get(task.task_type)
            if not handler:
                raise ValueError(f"不支持的任务类型: {task.task_type}")

            # 执行具体的爬取逻辑
            result = await handler(task, context)

            # 计算响应时间
            response_time = (datetime.now() - start_time).total_seconds()

            # 上报资源性能
            await self.proxy_service.report_resource_performance(
                context, result.success, response_time
            )

            # 记录结果
            if result.success:
                log.info("爬取任务执行成功",
                        task_id=task.task_id,
                        records_count=result.records_count,
                        response_time=response_time,
                        status_code=result.status_code)
            else:
                log.warning("爬取任务执行失败",
                           task_id=task.task_id,
                           error=result.error,
                           status_code=result.status_code,
                           response_time=response_time)

            return result.success

        except Exception as e:
            response_time = (datetime.now() - start_time).total_seconds()
            log.error("爬取任务异常",
                     task_id=task.task_id,
                     error=str(e),
                     response_time=response_time)
            return False

    async def _handle_cn_realtime_with_core(self, task: DragonflyTask, context: InjectionContext) -> CrawlingResult:
        """使用雪球核心引擎处理中国市场实时K线任务"""
        try:
            # 构建雪球核心引擎任务数据
            task_data = {
                "task_id": task.task_id,
                "endpoint": "kline",
                "symbol": task.symbol,
                "cookie_id": context.cookie_data.get("cookie_id", "") if context.cookie_data else "",
                "proxy": context.proxy_config.get("proxy_url") if context.proxy_config else None,
                "params": {
                    "symbol": task.symbol,
                    "period": self._convert_timeframe_for_xueqiu(task.task_type),
                    **task.payload  # 合并任务载荷中的额外参数
                }
            }

            # 使用雪球核心引擎执行任务
            result = await self.xueqiu_engine.execute_task_with_streams(task_data)

            # 转换结果格式
            return CrawlingResult(
                task_id=task.task_id,
                success=result.get("success", False),
                data=result.get("data"),
                error=result.get("error"),
                status_code=result.get("status_code"),
                response_time=result.get("response_time", 0.0),
                records_count=result.get("records_count", 0),
                metadata={
                    "market": "CN",
                    "source": "xueqiu_core",
                    "engine_version": "1.0.0",
                    **result.get("metadata", {})
                }
            )

        except Exception as e:
            log.error("雪球核心引擎处理失败", task_id=task.task_id, error=str(e))
            return CrawlingResult(
                task_id=task.task_id,
                success=False,
                error=f"xueqiu_core_error: {str(e)}"
            )

    async def _handle_cn_backfill_with_core(self, task: DragonflyTask, context: InjectionContext) -> CrawlingResult:
        """使用雪球核心引擎处理中国市场回填K线任务"""
        try:
            payload = task.payload
            start_date = payload.get("start_date")
            end_date = payload.get("end_date")

            # 构建雪球回填任务数据
            task_data = {
                "task_id": task.task_id,
                "endpoint": "kline",
                "symbol": task.symbol,
                "cookie_id": context.cookie_data.get("cookie_id", "") if context.cookie_data else "",
                "proxy": context.proxy_config.get("proxy_url") if context.proxy_config else None,
                "params": {
                    "symbol": task.symbol,
                    "period": self._convert_timeframe_for_xueqiu(task.task_type),
                    "begin": self._date_to_timestamp(end_date) if end_date else None,
                    "count": -1000,  # 批量获取历史数据
                    "type": "before",
                    **task.payload
                }
            }

            # 使用雪球核心引擎执行任务
            result = await self.xueqiu_engine.execute_task_with_streams(task_data)

            # 对于回填任务，可能需要额外的数据过滤
            if result.get("success") and start_date:
                result = await self._filter_backfill_data(result, start_date, end_date)

            return CrawlingResult(
                task_id=task.task_id,
                success=result.get("success", False),
                data=result.get("data"),
                error=result.get("error"),
                status_code=result.get("status_code"),
                response_time=result.get("response_time", 0.0),
                records_count=result.get("records_count", 0),
                metadata={
                    "market": "CN",
                    "source": "xueqiu_core",
                    "engine_version": "1.0.0",
                    "backfill_range": f"{start_date}~{end_date}",
                    **result.get("metadata", {})
                }
            )

        except Exception as e:
            log.error("雪球核心回填引擎处理失败", task_id=task.task_id, error=str(e))
            return CrawlingResult(
                task_id=task.task_id,
                success=False,
                error=f"xueqiu_core_backfill_error: {str(e)}"
            )

    def _convert_timeframe_for_xueqiu(self, task_type: str) -> str:
        """将任务类型转换为雪球时间周期格式"""
        mapping = {
            "1m_realtime": "1m",
            "5m_realtime": "5m",
            "15m_realtime": "15m",
            "15m_backfill": "15m",
            "1d_backfill": "day"
        }
        return mapping.get(task_type, "day")

    def _date_to_timestamp(self, date_str: str) -> int:
        """将日期字符串转换为时间戳(毫秒)"""
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            return int(dt.timestamp() * 1000)
        except:
            return int(datetime.now().timestamp() * 1000)

    async def _filter_backfill_data(self, result: Dict[str, Any], start_date: str, end_date: str) -> Dict[str, Any]:
        """过滤回填数据的日期范围"""
        try:
            if not result.get("success") or not result.get("data"):
                return result

            data = result["data"]
            if "item" not in data or not isinstance(data["item"], list):
                return result

            start_ts = self._date_to_timestamp(start_date)
            end_ts = self._date_to_timestamp(end_date)

            # 过滤时间范围内的数据点
            filtered_items = [
                item for item in data["item"]
                if isinstance(item, list) and len(item) > 0 and start_ts <= item[0] <= end_ts
            ]

            # 更新数据和记录数
            result["data"]["item"] = filtered_items
            result["records_count"] = len(filtered_items)

            return result

        except Exception as e:
            log.warning("过滤回填数据失败", error=str(e))
            return result

    async def _handle_us_realtime_kline(self, task: DragonflyTask, context: InjectionContext) -> CrawlingResult:
        """获取美国市场实时K线"""
        try:
            client = await self.proxy_service.create_http_client(context)

            # Yahoo Finance API参数
            params = {
                "interval": self._convert_timeframe_to_yahoo(timeframe),
                "range": "1d",
                "includePrePost": "true"
            }

            api_url = self.market_endpoints["US"]["kline_api"].format(symbol=symbol)

            async with client:
                response = await client.get(api_url, params=params)

                if response.status_code == 200:
                    data = response.json()

                    chart = data.get("chart", {})
                    result = chart.get("result", [])

                    if result:
                        chart_data = result[0]
                        timestamps = chart_data.get("timestamp", [])
                        indicators = chart_data.get("indicators", {})
                        quote = indicators.get("quote", [{}])[0]

                        return CrawlingResult(
                            task_id=task.task_id,
                            success=True,
                            data={
                                "timestamps": timestamps,
                                "quote": quote,
                                "meta": chart_data.get("meta", {})
                            },
                            status_code=response.status_code,
                            response_time=response.elapsed.total_seconds(),
                            records_count=len(timestamps),
                            metadata={
                                "market": "US",
                                "symbol": symbol,
                                "timeframe": timeframe,
                                "source": "yahoo_finance"
                            }
                        )
                    else:
                        return CrawlingResult(
                            task_id=task.task_id,
                            success=False,
                            error="Yahoo Finance API返回空数据",
                            status_code=response.status_code
                        )
                else:
                    return CrawlingResult(
                        task_id=task.task_id,
                        success=False,
                        error=f"HTTP错误: {response.status_code}",
                        status_code=response.status_code
                    )

        except Exception as e:
            return CrawlingResult(
                task_id=task.task_id,
                success=False,
                error=str(e)
            )

    async def _fetch_us_backfill_kline(self, task: DragonflyTask, context: InjectionContext,
                                     symbol: str, timeframe: str, start_date: str, end_date: str) -> CrawlingResult:
        """获取美国市场回填K线"""
        try:
            client = await self.proxy_service.create_http_client(context)

            # 转换日期为时间戳
            start_timestamp = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp())
            end_timestamp = int(datetime.strptime(end_date, "%Y-%m-%d").timestamp())

            params = {
                "interval": self._convert_timeframe_to_yahoo(timeframe),
                "period1": start_timestamp,
                "period2": end_timestamp,
                "includePrePost": "true"
            }

            api_url = self.market_endpoints["US"]["kline_api"].format(symbol=symbol)

            async with client:
                response = await client.get(api_url, params=params)

                if response.status_code == 200:
                    data = response.json()

                    chart = data.get("chart", {})
                    result = chart.get("result", [])

                    if result:
                        chart_data = result[0]
                        timestamps = chart_data.get("timestamp", [])
                        indicators = chart_data.get("indicators", {})
                        quote = indicators.get("quote", [{}])[0]

                        return CrawlingResult(
                            task_id=task.task_id,
                            success=True,
                            data={
                                "timestamps": timestamps,
                                "quote": quote,
                                "meta": chart_data.get("meta", {})
                            },
                            status_code=response.status_code,
                            response_time=response.elapsed.total_seconds(),
                            records_count=len(timestamps),
                            metadata={
                                "market": "US",
                                "symbol": symbol,
                                "timeframe": timeframe,
                                "start_date": start_date,
                                "end_date": end_date,
                                "source": "yahoo_finance"
                            }
                        )
                    else:
                        return CrawlingResult(
                            task_id=task.task_id,
                            success=False,
                            error="Yahoo Finance API返回空数据",
                            status_code=response.status_code
                        )
                else:
                    return CrawlingResult(
                        task_id=task.task_id,
                        success=False,
                        error=f"HTTP错误: {response.status_code}",
                        status_code=response.status_code
                    )

        except Exception as e:
            return CrawlingResult(
                task_id=task.task_id,
                success=False,
                error=str(e)
            )

    async def _fetch_hk_realtime_kline(self, task: DragonflyTask, context: InjectionContext,
                                     symbol: str, timeframe: str) -> CrawlingResult:
        """获取香港市场实时K线"""
        # 实现香港市场K线获取逻辑
        return CrawlingResult(
            task_id=task.task_id,
            success=False,
            error="香港市场K线获取尚未实现"
        )

    async def _fetch_hk_backfill_kline(self, task: DragonflyTask, context: InjectionContext,
                                     symbol: str, timeframe: str, start_date: str, end_date: str) -> CrawlingResult:
        """获取香港市场回填K线"""
        # 实现香港市场回填K线获取逻辑
        return CrawlingResult(
            task_id=task.task_id,
            success=False,
            error="香港市场回填K线获取尚未实现"
        )

    def _convert_timeframe_to_yahoo(self, timeframe: str) -> str:
        """转换时间周期到Yahoo Finance格式"""
        mapping = {
            "1m": "1m",
            "5m": "5m",
            "15m": "15m",
            "1h": "1h",
            "1d": "1d"
        }
        return mapping.get(timeframe, "1d")

    async def shutdown(self):
        """关闭爬虫引擎"""
        try:
            # 关闭雪球核心引擎
            await self.xueqiu_engine.shutdown()

            log.info("爬虫引擎已关闭")
        except Exception as e:
            log.error("关闭爬虫引擎失败", error=str(e))