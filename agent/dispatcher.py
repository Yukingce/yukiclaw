"""
    agent/dispatcher.py —— DevMate 团队任务编排入口

    干的事：构建团队 agent（含沙箱）→ 异步跑一个 Issue → 无论成败都清理沙箱。
    这是"一个 Issue 进、结果出"的统一入口，后面把它接进任务队列，
    做成后台长任务（异步排队执行）。
"""
from __future__ import annotations

from langchain_core.utils.uuid import uuid7   # uuid7 = 带时间戳、可排序的 UUID

from agent.main import build_team_agent
from infra.logging import get_logger

logger = get_logger()


async def run_issue(
    issue: str,
    user_id: str = "anonymous",
    channel: str = "cli",
    fetch_paths: list[str] | None = None,
) -> dict:
    """跑一个 Issue：构建团队 + 沙箱 → 委派子代理协作 →（可选）取回产物 → 最后清理沙箱。

    参数：
      - issue        用户的需求/任务描述（会作为第一条 user 消息喂给 agent）；
      - user_id      请求方标识，透传给中间件做上下文/审计；
      - channel      来源渠道（cli / web 等），同样透传给中间件；
      - fetch_paths  可选，一组沙箱内文件路径；传了就在【关沙箱前】把它们下载回来，
                     放进返回值的 "artifacts" 键，用来验证"Agent 是否真的在沙箱里改了文件"。

    返回：agent 的最终状态 dict（含 messages / todos 等）；
          若传了 fetch_paths，还会多出一个 "artifacts" 键。
    """
    # download_artifacts 也延迟 import（和 manager 里同理，按需加载，避免模块顶层强依赖）。
    from sandbox.manager import download_artifacts

    # 每个 Issue 用一个全新的 thread_id → 对应一个【独立沙箱】，会话之间互不干扰。
    thread_id = str(uuid7())
    agent, sandbox, client = build_team_agent(thread_id, user_id=user_id, channel=channel)

    try:
        # 真正跑 agent：把 issue 作为第一条 user 消息，按 thread_id 关联这次会话的状态
        # （checkpointer 靠 thread_id 找到/保存对应的对话与 todos）。
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": issue}]},
            config={"configurable": {"thread_id": thread_id}},
        )
        logger.info("Issue 处理完成：thread_id={}", thread_id)

        # 在关闭沙箱【之前】把产物取回来——沙箱一旦 stop/delete，里面的文件就拿不到了。
        if fetch_paths:
            # 这里需要一个 backend 来执行 download，但 agent 对象不直接暴露它，
            # 所以用同一个 sandbox 现场再包一个轻量 backend 专门用来下载。
            from langchain_daytona import DaytonaSandbox
            artifacts = download_artifacts(DaytonaSandbox(sandbox=sandbox), fetch_paths)
            result["artifacts"] = artifacts   # 形如 {沙箱内路径: 文件 bytes}
        return result
    finally:
        # ★ 关键：无论成功还是抛异常，都要回收沙箱，避免资源泄漏 / 持续计费（官方反复强调）。
        #   放在 finally 里，保证即使上面 ainvoke 报错，也一定会执行到这里。
        try:
            sandbox.stop()   # 停止沙箱（配合 manager 里 auto_delete_interval=60，停够 60min 会被自动删除）
            logger.info("沙箱已回收：thread_id={}", thread_id)
        except Exception as e:  # noqa: BLE001 回收失败也别让它盖住主流程的返回值/原始异常
            logger.warning("沙箱回收失败（thread_id={}）：{}", thread_id, e)