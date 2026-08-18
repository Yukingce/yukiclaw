"""
    tools/search_tools.py —— DevMate 的检索工具

    把"联网搜索 / 取网页内容"封装成工具。这里给出最简形态（你可换成 Tavily/Exa 等）。
    I/O 型工具用 async，避免阻塞事件循环。
"""

import html
import json
import re
import httpx
from fastmcp import FastMCP
from langchain.tools import tool
from infra.settings import get_settings

setting = get_settings()

# mcp = FastMCP("search-service")

TAVILY_URL = "https://api.tavily.com/search"

@tool()
async def web_search(query: str, max_results: int = 5) -> str:
    """
    联网搜索，返回与 query 最相关的若干结果摘要。用于需要查阅外部最新信息（如某库的最新用法、报错原因）时。

    Args:
        query: 搜索关键词.
        max_results: 返回结果数量，默认5条，最多10条
    
    Returns:
        JSON 格式的搜索结果，包含 answer(摘要) 和 results(详细结果列表)
    """
    tavily_api_key = setting.tavily_api_key
    # SecretStr 要真正用时才解开（与 agent/main.py 一致），否则 json 序列化会报错
    tavily_api_key = tavily_api_key.get_secret_value() if tavily_api_key else None

    if not tavily_api_key:
        return json.dumps({"error": "未配置 TAVILY_API_KEY"}, ensure_ascii=False)
    
    max_results = min(max_results, 10)
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                TAVILY_URL,
                json={
                    "api_key": tavily_api_key,
                    "query": query,
                    "search_depth": "basic",
                    "max_results": max_results,
                    "include_answer": True,
                }
            )
                #client.post：Tavily 要求使用 POST 请求，并将参数放在 json 体中发送。
                #search_depth="basic"：Tavily 提供 basic 和 advanced 两种深度，基础搜索速度更快。
                #include_answer=True：这是 Tavily 的特色，它会直接给出一个针对问题的摘要回答，而不仅仅是网页链接。
            
            if response.status_code != 200:
                return json.dumps({
                    "error": f"API 请求失败: {response.status_code}"
                }, ensure_ascii=False)
            
            data = response.json() #转回python字典
            
            result = {
                "query": query,
                "answer": data.get("answer"),
                "results": [
                    {
                        "title": r.get("title"),
                        "url": r.get("url"),
                        "content": r.get("content", "")[:300] # 截断处理
                    }
                    for r in data.get("results", [])
                ]
            }
            
            return json.dumps(result, ensure_ascii=False, indent=2)
            
        except httpx.TimeoutException:
            return json.dumps({"error": "请求超时"}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


@tool
async def fetch_url(url: str) -> str:
    """抓取给定 URL 的网页正文，用于阅读某个具体文档/页面。"""
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        try:
            response = await client.get(url)

            if response.status_code != 200:
                return json.dumps({"error": f"请求失败: {response.status_code}"}, ensure_ascii=False)

            # 抽取正文：去掉 script/style，剥掉 HTML 标签，还原实体，压平多余空白
            text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", response.text, flags=re.S | re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            text = html.unescape(text)
            text = re.sub(r"\s+", " ", text).strip()

            return text[:5000]  # 截断，避免过长结果灌爆上下文

        except httpx.TimeoutException:
            return json.dumps({"error": "请求超时"}, ensure_ascii=False)
        except httpx.RequestError as e:
            return json.dumps({"error": f"请求失败: {e}"}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)