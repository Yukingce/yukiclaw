"""
    arq worker + 任务定义（方案二：生产级）

    三进程里的"worker"：独立运行，从 Redis 取任务执行。
    启动：uv run arq tasks.worker.WorkerSettings

    设计：on_startup 建一次持久化资源（saver/store），整个 worker 进程复用——

    任务量大：后台任务开始和 web 抢 CPU/内存，web 响应变慢；
    要独立扩展：想单独多开几个干活的进程（worker），而不动 web；
    要可靠重试：任务失败要自动重试 N 次（BackgroundTasks 失败就没了）；
    要削峰：突发大量任务时，让它们在队列里排队，worker 按能力消费，而不是一拥而上。

"""
import sys
import time
import asyncio
from arq.connections import RedisSettings

from channels.base import InboundMessage
from channels.handler import handle_message
from infra.persistence import open_persistence
from infra.settings import get_settings
from infra.logging import get_logger

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from datetime import datetime, timezone
from prometheus_client import start_http_server
from obs.metrics import AGENT_TASK_DURATION, AGENT_QUEUE_WAIT, AGENT_QUEUE_LENGTH

logger = get_logger()


async def process_issue(ctx: dict, payload: dict) -> dict:
    """
        arq 任务：处理一个 Issue。返回值会被 arq 存为任务结果（job.result() 可取）。
        ctx —— arq 自动注入，你从来没传过它，是 worker 启动时 on_startup 填好的共享资源字典。

        1. HTTP 请求打到 POST /jobs，jobs.py:49-54 把请求体构造成一个 dict：
            payload = {
                "channel": req_body.get("channel", "api"),
                "user_id": req_body.get("user_id", "anonymous"),
                "text": req_body["text"],
                "conversation_id": req_body.get("conversation_id", uuid4().hex),
            }
        2. queue.py:16 入队时，把这个 dict 作为位置参数传给 arq：
            job = await redis.enqueue_job("process_issue", payload, _job_id=job_id)
        3. arq 把 payload 序列化（JSON）存进 Redis 队列。worker 取到后反序列化，原样作为第二个参数传回 process_issue

        
        从 payload（Redis 队列传来的 JSON）重建 InboundMessage（渠道、用户、文本、会话 ID）；
        从 ctx 取出启动时放进去的共享资源 checkpointer 和 store；
        调用 handle_message 真正处理消息，返回 reply；
        渠道分流：如果是飞书这种"推"型渠道，处理完主动 send 把回复推回去；webhook/web 这类"拉"型渠道则靠 arq 的 job.result() 查询结果。
        返回值 {"reply": ..., "conversation_id": ...} 会被 arq 存起来，供 web 端查询。
    """

    # 排队等待：用入队时塞进 payload 的 enqueued_at（不依赖 arq 内部字段）
    enqueued_at = payload.get("enqueued_at")
    if enqueued_at:
        try:
            wait = (datetime.now(timezone.utc)
                    - datetime.fromisoformat(enqueued_at)).total_seconds()
            AGENT_QUEUE_WAIT.observe(max(0.0, wait))
        except Exception:  # noqa: BLE001 时间戳异常不影响主流程
            pass

    start = time.perf_counter()

    inbound = InboundMessage(
        channel=payload["channel"], user_id=payload["user_id"],
        text=payload["text"], conversation_id=payload["conversation_id"],
    )
    try:
        reply = await handle_message(
            inbound, ctx["checkpointer"], ctx["store"],
            tenant_id=payload.get("tenant_id", "default"),
        )
        return {"reply": reply, "conversation_id": inbound.conversation_id}
    finally:
        # 端到端执行耗时（用户真正等的时间）
        AGENT_TASK_DURATION.observe(time.perf_counter() - start)


async def update_queue_length(ctx: dict):
    """周期性把队列长度写进 Gauge。"""
    try:
        jobs = await ctx["redis"].queued_jobs()      # arq 取待处理任务
        AGENT_QUEUE_LENGTH.set(len(jobs))
    except Exception as e:  # noqa: BLE001
        logger.warning("采集队列长度失败：{}", e)


async def on_startup(ctx: dict):
    """
        on_startup：进程启动时建一次持久化资源（连接池、checkpointer、store），
        放进 ctx 供整个 worker 进程复用——这就是 docstring 说的"复用资源"的设计，避免每个任务都重新连数据库。
    """
    pool, checkpointer, store = await open_persistence()
    ctx["pg_pool"], ctx["checkpointer"], ctx["store"] = pool, checkpointer, store
    logger.info("arq worker 启动：持久化资源就绪")


async def on_shutdown(ctx: dict):
    """
        关闭时优雅关掉数据库连接池
    """
    if ctx.get("pg_pool"):
        await ctx["pg_pool"].close()


class WorkerSettings:
    """
        arq 通过这个类发现配置。

        functions：注册了哪些任务（目前只有 process_issue）；
        on_startup / on_shutdown：挂上生命周期钩子；
        redis_settings：复用项目已有的 Redis（从 get_settings().redis_url 读 DSN）。
    
        跑：uv run arq tasks.worker.WorkerSettings
    """
    functions = [process_issue]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)  # 用已有 Redis
    from arq import cron
    cron_jobs = [cron(update_queue_length, second=set(range(0, 60, 5)))]