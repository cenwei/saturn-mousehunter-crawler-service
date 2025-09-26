#!/usr/bin/env python3
"""
爬虫功能测试脚本
测试 Cookie获取、代理获取和雪球数据抓取功能
"""
import asyncio
import json
import sys
import os
from typing import Dict, Any

# 添加项目路径
sys.path.append('/home/cenwei/workspace/saturn_mousehunter/saturn-mousehunter-crawler-service/src')

from application.services.xueqiu_core_engine import XueqiuCoreEngine
from infrastructure.settings.config import CrawlerSettings


async def test_cookie_acquisition():
    """测试Cookie获取功能"""
    print("\n🍪 测试Cookie获取功能...")

    try:
        # 1. 先从Cookie池API获取真实Cookie
        print("📡 从Cookie池API获取雪球Cookie...")
        import httpx
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "http://192.168.8.168:8000/api/v1/md/cookie/request",
                json={"website": "xueqiu.com", "usage_type": "crawling"}
            )

            if response.status_code == 200:
                cookie_response = response.json()
                cookie_id = cookie_response["cookie_id"]
                cookie_data = cookie_response["cookie_data"]

                # 构建雪球cookie字符串
                cookie_parts = []
                for key, value in cookie_data.items():
                    cookie_parts.append(f"{key}={value}")
                cookie_string = "; ".join(cookie_parts)

                print(f"✅ 从Cookie池获取成功 [池ID: {cookie_id[:8]}...]")
                print(f"   Cookie内容: {cookie_string[:50]}...")
                return cookie_id, cookie_string
            else:
                print(f"⚠️  Cookie池API调用失败: {response.status_code}")

        # 2. 如果Cookie池失败，尝试Dragonfly获取
        settings = CrawlerSettings()
        engine = XueqiuCoreEngine(settings)
        await engine.initialize()

        test_cookie_ids = ["xueqiu_session_001", "xueqiu_session", "default"]

        for cookie_id in test_cookie_ids:
            cookie = await engine._get_cookie(cookie_id)
            if cookie:
                print(f"✅ Dragonfly获取成功 [{cookie_id}]: {cookie[:50]}...")
                return cookie_id, cookie
            else:
                print(f"❌ Dragonfly获取失败 [{cookie_id}]: None")

        # 3. 如果都没有找到Cookie，使用模拟Cookie
        print("⚠️  未找到现有Cookie，使用模拟Cookie进行测试")
        test_cookie = "xq_a_token=test123; xq_r_token=test456; xq_id_token=test789"
        return "test_mock", test_cookie

    except Exception as e:
        print(f"❌ Cookie获取测试失败: {str(e)}")
        return None, None


async def test_proxy_acquisition():
    """测试代理获取功能"""
    print("\n🌐 测试代理获取功能...")

    try:
        settings = CrawlerSettings()
        engine = XueqiuCoreEngine(settings)
        await engine.initialize()

        # 测试获取代理
        proxy = await engine._get_random_proxy()

        if proxy:
            print(f"✅ 代理获取成功: {proxy}")
            return proxy
        else:
            print("⚠️  未找到可用代理，使用直连模式")
            return None

    except Exception as e:
        print(f"❌ 代理获取测试失败: {str(e)}")
        return None


async def test_direct_http_crawling(symbol: str, cookie: str, proxy: str = None):
    """测试直接HTTP抓取功能（不依赖Dragonfly）"""
    print(f"\n📊 测试直接抓取股票数据 [{symbol}]...")

    try:
        import httpx
        import time

        # 构建雪球API请求
        url = "https://stock.xueqiu.com/v5/stock/chart/kline.json"

        params = {
            "symbol": symbol,
            "begin": int(time.time() * 1000),  # 当前时间戳
            "period": "day",
            "type": "before",
            "count": -5,  # 最近5条数据
            "indicator": "kline,pe,pb,ps,pcf,market_capital"
        }

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": f"https://xueqiu.com/S/{symbol}",
            "Origin": "https://xueqiu.com",
            "X-Requested-With": "XMLHttpRequest",
            "Cookie": cookie
        }

        # 配置代理
        proxy_config = None
        if proxy:
            proxy_config = proxy
            print(f"🔄 使用代理: {proxy}")
        else:
            print("🔄 使用直连模式")

        timeout = httpx.Timeout(
            connect=10.0,
            read=30.0,
            write=10.0,
            pool=5.0
        )

        async with httpx.AsyncClient(
            timeout=timeout,
            proxy=proxy_config,
            follow_redirects=True
        ) as client:
            response = await client.get(url, params=params, headers=headers)

            print(f"📈 HTTP响应状态: {response.status_code}")

            if response.status_code == 200:
                data = response.json()

                if data.get("error_code") == 0:
                    kline_data = data.get("data", {})
                    items = kline_data.get("item", [])

                    print(f"✅ 数据抓取成功!")
                    print(f"   股票代码: {symbol}")
                    print(f"   数据条数: {len(items)}")
                    print(f"   响应时间: {response.elapsed.total_seconds():.2f}秒")

                    # 显示部分数据结构
                    if items:
                        print(f"   最新数据点: {items[0][:6] if len(items[0]) >= 6 else items[0]}")
                        print(f"   数据字段: timestamp, open, high, low, close, volume...")

                    # 返回结构化结果
                    return {
                        "success": True,
                        "symbol": symbol,
                        "records_count": len(items),
                        "response_time": response.elapsed.total_seconds(),
                        "status_code": response.status_code,
                        "sample_data": items[:2] if items else [],
                        "full_response": data
                    }
                else:
                    error_msg = data.get("error_description", "未知错误")
                    print(f"❌ 雪球API返回错误: {error_msg}")
                    return {
                        "success": False,
                        "error": f"api_error: {error_msg}",
                        "status_code": response.status_code,
                        "response_data": data
                    }
            else:
                print(f"❌ HTTP请求失败: {response.status_code}")
                return {
                    "success": False,
                    "error": f"http_error: {response.status_code}",
                    "status_code": response.status_code,
                    "response_text": response.text[:500]
                }

    except Exception as e:
        print(f"❌ 直接抓取测试失败: {str(e)}")
        return {
            "success": False,
            "error": f"exception: {str(e)}"
        }


async def test_xueqiu_core_engine(symbol: str, cookie_id: str, cookie: str, proxy: str = None):
    """测试雪球核心引擎功能"""
    print(f"\n🚀 测试雪球核心引擎 [{symbol}]...")

    try:
        settings = CrawlerSettings()
        engine = XueqiuCoreEngine(settings)
        await engine.initialize()

        # 构建任务数据
        task_data = {
            "task_id": f"test_{symbol}_{int(time.time())}",
            "endpoint": "kline",
            "symbol": symbol,
            "cookie_id": cookie_id,
            "proxy": proxy,
            "params": {
                "symbol": symbol,
                "period": "day",
                "count": -5,
                "type": "before"
            }
        }

        # 由于没有真实的Dragonfly连接，我们需要mock Cookie获取
        # 临时替换_get_cookie方法
        original_get_cookie = engine._get_cookie
        async def mock_get_cookie(cid):
            return cookie if cid == cookie_id else None
        engine._get_cookie = mock_get_cookie

        # 执行任务
        result = await engine.execute_task_with_streams(task_data)

        # 恢复原方法
        engine._get_cookie = original_get_cookie

        if result.get("success"):
            print(f"✅ 雪球引擎测试成功!")
            print(f"   任务ID: {result.get('task_id')}")
            print(f"   记录数: {result.get('records_count', 0)}")
            print(f"   响应时间: {result.get('response_time', 0):.2f}秒")
            return result
        else:
            print(f"❌ 雪球引擎测试失败: {result.get('error')}")
            return result

    except Exception as e:
        print(f"❌ 雪球核心引擎测试异常: {str(e)}")
        return {"success": False, "error": str(e)}


async def run_comprehensive_test():
    """运行综合测试"""
    print("🧪 Saturn MouseHunter 爬虫功能综合测试")
    print("=" * 60)

    # 测试股票代码
    test_symbol = "SH600000"  # 浦发银行

    # 1. 测试Cookie获取
    cookie_id, cookie = await test_cookie_acquisition()
    if not cookie:
        print("\n❌ 测试终止: 无法获取Cookie")
        return

    # 2. 测试代理获取
    proxy = await test_proxy_acquisition()

    # 3. 测试直接HTTP抓取
    direct_result = await test_direct_http_crawling(test_symbol, cookie, proxy)

    # 4. 测试雪球核心引擎（如果直接抓取成功）
    if direct_result.get("success"):
        engine_result = await test_xueqiu_core_engine(test_symbol, cookie_id, cookie, proxy)
    else:
        print("\n⚠️  跳过核心引擎测试（直接抓取失败）")
        engine_result = None

    # 5. 生成测试报告
    print("\n" + "=" * 60)
    print("📋 测试结果总结:")
    print(f"   Cookie获取: {'✅ 成功' if cookie else '❌ 失败'}")
    print(f"   代理获取: {'✅ 成功' if proxy else '⚠️  无代理'}")
    print(f"   直接抓取: {'✅ 成功' if direct_result.get('success') else '❌ 失败'}")
    print(f"   核心引擎: {'✅ 成功' if engine_result and engine_result.get('success') else '❌ 失败' if engine_result else '⚠️  跳过'}")

    # 保存测试结果
    test_report = {
        "test_timestamp": time.time(),
        "test_symbol": test_symbol,
        "cookie_available": bool(cookie),
        "proxy_available": bool(proxy),
        "direct_crawling_result": direct_result,
        "core_engine_result": engine_result,
        "summary": {
            "cookie_test": "success" if cookie else "failed",
            "proxy_test": "success" if proxy else "no_proxy",
            "direct_crawling_test": "success" if direct_result.get("success") else "failed",
            "core_engine_test": "success" if engine_result and engine_result.get("success") else "failed"
        }
    }

    # 保存测试结果到文件
    report_file = "/home/cenwei/workspace/saturn_mousehunter/saturn-mousehunter-crawler-service/crawler_test_report.json"
    with open(report_file, 'w', encoding='utf-8') as f:
        json.dump(test_report, f, ensure_ascii=False, indent=2)

    print(f"\n📁 测试报告已保存: {report_file}")

    return test_report


if __name__ == "__main__":
    import time
    asyncio.run(run_comprehensive_test())