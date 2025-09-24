"""
爬虫引擎
集成代理注入的HTTP数据抓取引擎
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
    """爬虫引擎"""

    def __init__(self, proxy_service: ProxyIntegrationService, settings: Optional[CrawlerSettings] = None):
        self.proxy_service = proxy_service
        self.settings = settings or CrawlerSettings()

        # 任务处理器映射
        self.task_handlers = {
            "1m_realtime": self._handle_realtime_kline,
            "5m_realtime": self._handle_realtime_kline,
            "15m_realtime": self._handle_realtime_kline,
            "15m_backfill": self._handle_backfill_kline,
            "1d_backfill": self._handle_backfill_kline
        }

        # 市场端点配置
        self.market_endpoints = {
            "CN": {
                "kline_api": "https://stock.xueqiu.com/v5/stock/chart/kline.json",
                "quote_api": "https://stock.xueqiu.com/v5/stock/quote.json",
                "batch_quote_api": "https://stock.xueqiu.com/v5/stock/batch/quote.json"
            },
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
            log.info("爬虫引擎初始化成功",
                    supported_markets=list(self.market_endpoints.keys()),
                    supported_task_types=list(self.task_handlers.keys()))

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

    async def _handle_realtime_kline(self, task: DragonflyTask, context: InjectionContext) -> CrawlingResult:
        """处理实时K线任务"""
        try:
            # 构建实时K线请求参数
            payload = task.payload
            symbol = task.symbol
            timeframe = task.timeframe

            # 根据市场构建不同的请求
            if task.market == "CN":
                return await self._fetch_cn_realtime_kline(task, context, symbol, timeframe)
            elif task.market == "US":
                return await self._fetch_us_realtime_kline(task, context, symbol, timeframe)
            elif task.market == "HK":
                return await self._fetch_hk_realtime_kline(task, context, symbol, timeframe)
            else:
                raise ValueError(f"不支持的市场: {task.market}")

        except Exception as e:
            return CrawlingResult(
                task_id=task.task_id,
                success=False,
                error=str(e)
            )

    async def _handle_backfill_kline(self, task: DragonflyTask, context: InjectionContext) -> CrawlingResult:
        """处理回填K线任务"""
        try:
            payload = task.payload
            symbol = task.symbol
            timeframe = task.timeframe
            start_date = payload.get("start_date")
            end_date = payload.get("end_date")

            # 根据市场构建不同的请求
            if task.market == "CN":
                return await self._fetch_cn_backfill_kline(task, context, symbol, timeframe, start_date, end_date)
            elif task.market == "US":
                return await self._fetch_us_backfill_kline(task, context, symbol, timeframe, start_date, end_date)
            elif task.market == "HK":
                return await self._fetch_hk_backfill_kline(task, context, symbol, timeframe, start_date, end_date)
            else:
                raise ValueError(f"不支持的市场: {task.market}")

        except Exception as e:
            return CrawlingResult(
                task_id=task.task_id,
                success=False,
                error=str(e)
            )

    async def _fetch_cn_realtime_kline(self, task: DragonflyTask, context: InjectionContext,
                                     symbol: str, timeframe: str) -> CrawlingResult:
        """获取中国市场实时K线"""
        try:
            # 创建HTTP客户端
            client = await self.proxy_service.create_http_client(context)

            # 构建雪球API参数
            params = {
                "symbol": symbol,
                "begin": int(datetime.now().timestamp() * 1000) - 24*60*60*1000,  # 24小时前
                "period": timeframe,
                "type": "before",
                "count": -100,  # 最近100条
                "indicator": "kline,pe,pb,ps,pcf,market_capital,agt,ggt,balance"
            }

            api_url = self.market_endpoints["CN"]["kline_api"]

            async with client:
                response = await client.get(api_url, params=params)

                if response.status_code == 200:
                    data = response.json()

                    # 解析雪球K线数据
                    if data.get("error_code") == 0 and "data" in data:
                        kline_data = data["data"]
                        item_count = len(kline_data.get("item", []))

                        return CrawlingResult(
                            task_id=task.task_id,
                            success=True,
                            data=kline_data,
                            status_code=response.status_code,
                            response_time=response.elapsed.total_seconds(),
                            records_count=item_count,
                            metadata={
                                "market": "CN",
                                "symbol": symbol,
                                "timeframe": timeframe,
                                "source": "xueqiu"
                            }
                        )
                    else:
                        return CrawlingResult(
                            task_id=task.task_id,
                            success=False,
                            error=f"API返回错误: {data.get('error_description', '未知错误')}",
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

    async def _fetch_cn_backfill_kline(self, task: DragonflyTask, context: InjectionContext,
                                     symbol: str, timeframe: str, start_date: str, end_date: str) -> CrawlingResult:
        """获取中国市场回填K线"""
        try:
            client = await self.proxy_service.create_http_client(context)

            # 转换日期格式
            start_timestamp = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp() * 1000)
            end_timestamp = int(datetime.strptime(end_date, "%Y-%m-%d").timestamp() * 1000)

            params = {
                "symbol": symbol,
                "begin": end_timestamp,
                "period": timeframe,
                "type": "before",
                "count": -1000,  # 批量获取
                "indicator": "kline,pe,pb,ps,pcf,market_capital,agt,ggt,balance"
            }

            api_url = self.market_endpoints["CN"]["kline_api"]

            async with client:
                response = await client.get(api_url, params=params)

                if response.status_code == 200:
                    data = response.json()

                    if data.get("error_code") == 0 and "data" in data:
                        kline_data = data["data"]
                        items = kline_data.get("item", [])

                        # 过滤日期范围内的数据
                        filtered_items = [
                            item for item in items
                            if start_timestamp <= item[0] <= end_timestamp
                        ]

                        return CrawlingResult(
                            task_id=task.task_id,
                            success=True,
                            data={**kline_data, "item": filtered_items},
                            status_code=response.status_code,
                            response_time=response.elapsed.total_seconds(),
                            records_count=len(filtered_items),
                            metadata={
                                "market": "CN",
                                "symbol": symbol,
                                "timeframe": timeframe,
                                "start_date": start_date,
                                "end_date": end_date,
                                "source": "xueqiu"
                            }
                        )
                    else:
                        return CrawlingResult(
                            task_id=task.task_id,
                            success=False,
                            error=f"API返回错误: {data.get('error_description', '未知错误')}",
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

    async def _fetch_us_realtime_kline(self, task: DragonflyTask, context: InjectionContext,
                                     symbol: str, timeframe: str) -> CrawlingResult:
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
            log.info("爬虫引擎已关闭")
        except Exception as e:
            log.error("关闭爬虫引擎失败", error=str(e))