"""
    方式二：MCP 统一管理工具
    MCP 客户端管理器
    统一管理所有 MCP 服务连接
"""
import asyncio
import os
from typing import Optional, List
from langchain_mcp_adapters.client import MultiServerMCPClient
from infra.logging import get_logger
from infra.settings import get_settings

logger = get_logger()
setting = get_settings()


from pathlib import Path

class MCPClientManager:
    """
    MCP 客户端管理器（单例模式）
    """

    _instance: Optional['MCPClientManager'] = None
    _client: Optional[MultiServerMCPClient] = None
    _tools: Optional[List] = None
    _lock = asyncio.Lock()

    # 项目根目录（用于 stdio 服务）
    PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))

    # 环境变量（追加 PYTHONPATH）
    ENV_VARS = os.environ.copy()
    ENV_VARS["PYTHONPATH"] = PROJECT_ROOT + os.pathsep + ENV_VARS.get("PYTHONPATH", "")

    # 服务器配置(可以放到settings中，统一配置)
    SERVER_CONFIGS = {
        # ========== 自建服务（stdio） ==========
        "example_0": {
            "command": "python",
            "args": ["-m", "local_address"],
            "transport": "stdio",
            "env": ENV_VARS,
        },

        # ========== 外部服务（HTTP） ==========
        "example_1": {
            "url": "",
            "transport": "streamable_http",
        },
    }

    @classmethod
    async def get_instance(cls, servers: List[str] = None) -> 'MCPClientManager':
        """获取单例实例"""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
                    await cls._instance.initialize(servers=servers)
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """重置单例（用于测试）"""
        cls._instance = None

    async def initialize(self, servers: List[str] = None):
        """
        初始化 MCP 客户端

        Args:
            servers: 要启用的服务列表，默认启用所有
        """
        if self._client is not None:
            logger.warning("⚠️ MCP 客户端已初始化，跳过")
            return

        # 默认启用所有服务
        servers = servers or list(self.SERVER_CONFIGS.keys())
        configs = {k: v for k, v in self.SERVER_CONFIGS.items() if k in servers}

        logger.info(f"初始化 MCP: {list(configs.keys())}")

        # 创建客户端
        self._client = MultiServerMCPClient(configs)

        # 预加载工具
        try:
            self._tools = await self._client.get_tools()
            logger.info(f"✅ 已加载 {len(self._tools)} 个 MCP 工具")
        except Exception as e:
            logger.warning(f"⚠️ 预加载工具失败: {e}")
            self._tools = []

    async def close(self):
        """关闭客户端"""
        if self._client:
            self._client = None
            self._tools = None
            logger.info("MCP 客户端已关闭")

    #充当对外接口(懒加载策略)
    async def get_tools(self) -> List:
        """
        获取所有 MCP 工具

        Returns:
            LangChain 工具列表
        """
        if self._client is None:
            raise RuntimeError("MCP 客户端未初始化，请先调用 initialize()")

        # 如果已缓存，直接返回
        if self._tools:
            return self._tools

        # 否则重新获取
        self._tools = await self._client.get_tools()
        return self._tools


async def get_mcp_client(servers: List[str] = None) -> MCPClientManager:
    """获取 MCP 客户端管理器实例"""
    return await MCPClientManager.get_instance(servers)