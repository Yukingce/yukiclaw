"""
yukiClaw 的配置中枢

- 所有 API Key / Base URL / 连接串统一从这里取，不散落在业务代码里
- Pydantic Settings 做类型校验，缺失关键项时给清晰报错

"""
import os
from functools import cache
from typing import ClassVar, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置，并根据模型供应商解析各档位使用的模型。"""

    model_config = SettingsConfigDict(
        env_prefix="YUKI_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ===== 模型 =====
    MODEL_TIERS_BY_SUPPLIER: ClassVar[dict[str, dict[str, str]]] = {
        "deepseek": {
            "strong": "deepseek-v4-pro",
            "standard": "deepseek-v4-pro",
            "cheap": "deepseek-v4-flash",
        },
        "qwen": {
            "strong": "qwen3.8-max",
            "standard": "qwen3.8-plus",
            "cheap": "qwen-turbo",
        },
    }

    model_supplier: str = "deepseek"
    # 留空时按 model_supplier 自动选择；也可用 YUKI_MODEL_TIERS JSON 显式覆盖。
    model_tiers: dict[str, str] = Field(default_factory=dict)
    model_provider: str = "openai"
    api_key: SecretStr                    # 用 SecretStr，避免密钥被 print/log 泄露
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # ===== 连接弹性 =====
    max_retries: int = 12
    timeout: int = 60

    # ===== 持久化底座 =====
    postgres_url: str = "postgresql://yuking:yuking@localhost:5432/yukiclaw"
    pg_pool_min: int = 2
    pg_pool_max: int = 20

    redis_url: str = "redis://localhost:6379/0"
    redis_pool_max: int = 20

    # ===== MCP 工具 ======
    tavily_api_key: SecretStr | None = None

    # ===== 运行 =====
    log_level: str = "INFO"

    # ====== 飞书 =======
    feishu_app_id: str = ""
    feishu_app_secret: SecretStr = SecretStr("")
    feishu_encrypt_key: str = ""           # 长连接可留空；Webhook 回调模式才用
    feishu_verification_token: str = ""    # 同上

    # 网关多租户
    # 形如 {"key-tenant-a": "tenant-a", "key-tenant-b": "tenant-b"}
    api_keys: dict[str, str] = {}


    # ===== 沙箱隔离 =====
    sandbox_provider: str = "docker"        # docker（自托管）/ daytona
    sandbox_runtime: str = "runc"           # runc(加固容器) / runsc(gVisor) / kata(microVM)
    sandbox_image: str = "yuki-sandbox:latest"
    sandbox_workdir: str = "/home/AgentSandbox"    # 容器内工作目录（tmpfs，可写）
    sandbox_mem_limit: str = "512m"
    sandbox_pids_limit: int = 256
    sandbox_cpus: str = "1.0"
    sandbox_pool_size: int = 4              # 沙箱池并发上限

    # ===== 并发与限流 =====
    max_concurrent_llm: int = 8             # LLM 并发调用上限


    # ===== LangSmith 可观测=====
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr = SecretStr("")
    langsmith_project: str = "yukiclaw"
    langsmith_endpoint: str = "https://api.smith.langchain.com"



    @model_validator(mode="after")
    def resolve_model_tiers(self) -> Self:
        """按供应商填充模型档位，并校验路由所需档位是否完整。"""
        supplier = self.model_supplier.strip().lower()
        if supplier not in self.MODEL_TIERS_BY_SUPPLIER:
            supported = ", ".join(sorted(self.MODEL_TIERS_BY_SUPPLIER))
            raise ValueError(f"不支持的模型供应商：{supplier}；可选：{supported}")

        self.model_supplier = supplier
        if not self.model_tiers:
            self.model_tiers = self.MODEL_TIERS_BY_SUPPLIER[supplier].copy()

        required_tiers = {"strong", "standard", "cheap"}
        missing_tiers = required_tiers - self.model_tiers.keys()
        if missing_tiers:
            missing = ", ".join(sorted(missing_tiers))
            raise ValueError(f"model_tiers 缺少必要档位：{missing}")
        return self

def _export_langsmith_env(s: "Settings") -> None:
    """把 LangSmith 变量写回 os.environ。

    关键：LangSmith 库只认系统环境变量，不读 Settings 对象——所以必须写回 os.environ。
    只在开启 tracing 时写，避免没配 key 时设一堆空变量。
    """
    if not s.langsmith_tracing:
        return
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = s.langsmith_api_key.get_secret_value()
    os.environ["LANGSMITH_PROJECT"] = s.langsmith_project
    os.environ["LANGSMITH_ENDPOINT"] = s.langsmith_endpoint


# ===== 模块级执行：import 这个模块时就把环境变量写好 =====
# 放模块级（不只在 get_settings 里）是为了保证：任何代码一旦 import infra.settings，
# LangSmith 变量就立刻进环境，且通常早于 langchain/langgraph 真正使用追踪。
_settings = Settings()
_export_langsmith_env(_settings)

def get_settings() -> "Settings":
    """全局单例。"""
    return _settings


# 全进程单例的一种实现方式
# @cache
# def get_settings() -> Settings:
#     """全进程单例。业务代码统一通过它拿配置。"""
#     return Settings()
