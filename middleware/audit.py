"""
    DevMate 的工具审计中间件（异步）,记录工具调用时间

用 awrap_tool_call 包裹每次工具执行， 在 await handler(request) 前后各记一笔
不更新状态，不拦截，只进行观测

"""
import time
from collections.abc import Callable

from langchain.agents.middleware import AgentMiddleware
from langchain.messages import ToolMessage
from langchain.tools.tool_node import ToolCallRequest
from langgraph.types import Command

from infra.logging import get_logger
from obs.metrics import AGENT_TOOL_CALLS, AGENT_TOOL_DURATION

logger = get_logger()

class ToolAuditMiddleware(AgentMiddleware):
    """
        每次工具调用前后打印审计日志
    """

    async def awrap_tool_call(
            self, 
            request: ToolCallRequest, 
            handler: Callable[[ToolCallRequest], "Command | ToolMessage"]
        ) -> "Command | ToolMessage":
        # request 一般是一个 ToolCallRequest
        tool_name = request.tool_call["name"]

        start = time.perf_counter()

        status = "ok"
        try:
            return await handler(request)
        except Exception:
            status = "error"
            raise
        finally:
            elapsed = time.perf_counter() - start
            AGENT_TOOL_CALLS.labels(tool_name, status).inc()        # 埋点
            AGENT_TOOL_DURATION.labels(tool_name).observe(elapsed)  # 埋点，记录一次值
            logger.info("🔧 工具 {} {}（{:.0f} ms）", tool_name, status, elapsed * 1000)
        


