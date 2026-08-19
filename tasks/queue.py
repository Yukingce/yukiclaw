"""
     任务入队 + 结果查询（producer 侧，方案二）
"""
from arq import create_pool
from arq.connections import RedisSettings
from arq.jobs import Job, JobStatus

from infra.settings import get_settings
from datetime import datetime, timezone

async def get_arq_redis():
    """建 arq 的 Redis 连接池（producer 用）。lifespan 建一次复用。"""
    return await create_pool(RedisSettings.from_dsn(get_settings().redis_url))


async def enqueue_issue(redis, payload: dict, job_id: str | None = None) -> str:
    """
        任务入队，返回 job_id。job_id 用于幂等：同 id 不会重复入队。
    """
    payload = {**payload, "enqueued_at": datetime.now(timezone.utc).isoformat()}  # 自己塞时间戳
    job = await redis.enqueue_job("process_issue", payload, _job_id=job_id) 
    # 用 _job_id 保证幂等是这个异步模型下「入口放宽、执行侧兜底」思路的体现。
    return job.job_id if job is not None else job_id


async def get_job_result(redis, job_id: str) -> dict:
    """查任务状态/结果。"""
    job = Job(job_id=job_id, redis=redis)
    status = await job.status()
    out = {"job_id": job_id, "status": status.value if hasattr(status, "value") else str(status)}
    if status == JobStatus.complete:
        try:
            out["result"] = await job.result(timeout=1)
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)
    return out
