"""
    构建 DevMate 主 Agent
"""
from langchain.chat_models import init_chat_model
from deepagents import create_deep_agent

# 中间件
from middleware.audit import ToolAuditMiddleware # 工具调用时间
from middleware.context import RequestContextMiddleware # 记录用户信息
from middleware.cost import CostMeterMiddleware # 记录消耗的token
from middleware.concurrency import LLMConcurrencyMiddleware  # 限制 LLM 并发
from infra.fallback import build_fallback_middleware  # LLM 降级

# backend 文件系统
from langgraph.checkpoint.memory import MemorySaver
from subagents.profiles import build_subagents
from sandbox.manager import get_or_create_sandbox_backend

# tools 工具
from tools.local_tools_registry import get_tools

# model Router
from infra.llm_router import get_model

from infra.settings import get_settings
from infra.logging import get_logger


logger = get_logger()

# DevMate 的人设：只写"领域要求"，如何用 write_todos / 文件工具"这类通用本领由 DeepAgents 内置提示词负责。

DEVMATE_SYSTEM_PROMPT = """你是 DevMate，一个严谨的研发助手，服务于一个 Python / FastAPI 订单微服务团队。

工作准则：
- 动手前先用 write_todos 写出清晰的任务清单，并随进展更新它。
- 写代码遵循团队规范：类型注解齐全、函数职责单一、关键逻辑配 docstring。
- 不臆测：信息不足时先用 read_file / grep 读相关文件再动手。
- 每次改动后，用一两句话说明"改了什么、为什么这么改"。
"""


async def build_sandbox_backend():
    """按 SYC_SANDBOX_PROVIDER 选用沙箱后端，返回 (backend, workdir)。

    docker  → 自托管加固容器
    daytona → 外部托管沙箱
    两者都是 BaseSandbox，create_deep_agent(backend=...) 一视同仁。
    """
    s = get_settings()
    if s.sandbox_provider == "docker":
        from sandbox.docker_manager import create_one_sandbox, seed_project
        sb = await create_one_sandbox()
        await seed_project(sb)
        return sb, s.sandbox_workdir
    else:  # daytona
        from sandbox.manager import get_or_create_sandbox_backend
        backend, sandbox, client, workdir = get_or_create_sandbox_backend("default")
        return backend, workdir


from pathlib import Path
from deepagents.backends import FilesystemBackend
from subagents.reviewer import REVIEWER_PERMISSIONS
PROJECT_ROOT = Path(__file__).resolve().parent.parent

def build_agent(checkpointer=None, store=None, user_id="anonymous", channel="api"):
    """服务化版：checkpointer/store 由外部（lifespan）传入并复用。

    不传时退化为内存（仅供脚本/测试），传入则用 Postgres 持久化。
    """

    backend = FilesystemBackend(root_dir=str(PROJECT_ROOT), virtual_mode=True)
    return create_deep_agent(
        model=get_model("strong"),
        backend=backend,
        permissions=REVIEWER_PERMISSIONS, # FilesystemBackend 上的物理只读 reviewer
        system_prompt=DEVMATE_SYSTEM_PROMPT,
        skills=[str(PROJECT_ROOT / "skills")],
        memory=[str(PROJECT_ROOT / "AGENTS.md")],
        tools=get_tools("git", "search"),
        middleware=[
            RequestContextMiddleware(user_id=user_id, channel=channel),
            ToolAuditMiddleware(),
            CostMeterMiddleware(),
            LLMConcurrencyMiddleware(),
            build_fallback_middleware()
        ],
        checkpointer=checkpointer,      # ← 外部传入（lifespan 的 AsyncPostgresSaver）
        store=store,                    # ← 外部传入（lifespan 的 AsyncPostgresStore）
    )


def build_team_agent(thread_id: str, user_id: str = "anonymous", channel: str = "cli"):
    """构建 DevMate 主 Agent，返回一个已编译的 LangGraph 图。

    注意：创建只用 create_deep_agent（官方唯一工厂）。
    "异步"体现在调用阶段——我们之后用 agent.ainvoke / agent.astream。

    - backend 用沙箱（自动提供文件工具 + execute），取代第 4 章的 FilesystemBackend；
    - 沙箱在新建时已被 seed 进 app/ tests/ skills/ AGENTS.md（见 sandbox/manager.py）；
    - skills/memory 指向沙箱工作目录下 seed 后的真实路径（workdir/skills 等）；
    - subagents 注入五个专职子代理；
    - 密钥（API key）只在主进程的 build_model() 里用，绝不传进沙箱。
    返回 (agent, sandbox, client) —— 后两者用于用完后清理。
    """

    # workdir 是沙箱工作目录（Daytona 默认 /home/daytona），项目就 seed 在它下面
    sandbox_backend, sandbox, client, workdir = get_or_create_sandbox_backend(thread_id)

    agent = create_deep_agent(
        model=get_model("strong"),
        system_prompt=DEVMATE_SYSTEM_PROMPT + (
            f"\n\n【执行环境与路径规则——必须严格遵守】"
            f"\n你运行在一个沙箱里，项目代码已位于 `{workdir}/` 下（含 `{workdir}/app/`、`{workdir}/tests/`）。"
            f"\n⚠️ 所有文件操作（write_file/edit_file/read_file）和命令（execute）都【必须】使用以 `{workdir}/` 开头的【绝对路径】。"
            f"\n✅ 正确：写测试到 `{workdir}/tests/test_pricing.py`、改代码 `{workdir}/app/pricing.py`、"
            f"跑测试 `cd {workdir} && python -m pytest -q`。"
            f"\n❌ 错误（会因权限被拒绝，绝不要这样）：`/test_pricing.py`、`test_pricing.py`、`/app/pricing.py` 这类根路径或相对路径。"
            f"\n如果你不确定某文件在哪，先用 `ls {workdir}` 查看，再用绝对路径操作。"
            "\n\n你是团队负责人：对复杂 Issue，先用 task() 委派给 planner 规划，"
            "再依次委派 researcher/coder/tester/reviewer。你只做协调，不亲自写大量代码。"
            "委派时，把上面的【绝对路径规则】一并转达给子代理。"
        ),
        backend=sandbox_backend,                       # ← 沙箱后端：自动带 execute
        subagents=build_subagents(workdir),            # ← 五个专职子代理（把 workdir 传进去写进各自 prompt）
        # 路径相对 backend 的 root（= PROJECT_ROOT），用正斜杠（官方要求）
        skills=[f"{workdir}/skills"],                  # ← seed 后的真实路径（不再是空的 /skills/）
        memory=[f"{workdir}/AGENTS.md"],               # ← 同上
        tools=get_tools("git", "search", "test"),
        middleware=[
            RequestContextMiddleware(user_id, channel),
            ToolAuditMiddleware(),
            CostMeterMiddleware(tier="strong"),
            LLMConcurrencyMiddleware(),
            build_fallback_middleware()
        ], # 上下文 / 审计 / 成本 三条自定义中间件。 列表越靠前 = 越外层。
        checkpointer=MemorySaver(),
    )
    logger.info("DevMate 团队版构建完成：5 子代理 + 沙箱后端（项目已 seed 到 {}）", workdir)
    return agent, sandbox, client