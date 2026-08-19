"""
    方案二：arq 异步提交（入队即返回）+ 查结果
"""

import hashlib
from uuid import uuid4
from fastapi import APIRouter, Request
from gateway.limiter import limiter

router = APIRouter(prefix="/jobs", tags=["jobs-arq"])


"""
入队即返回」= 接口只负责「把活儿排上队、发个取件号(job_id)」，不负责「干活」；
活儿交给后台 worker 慢慢跑，客户端拿取件号来查。 入口「不校验、尽快丢队列」就是为了
让 web 层能秒回、不阻塞，把执行和（部分）校验成本移到后台 worker 进程。

一、典型的 生产者/消费者（producer/consumer） 架构：

    HTTP 请求（producer）──入队──▶ Redis 队列 ──消费──▶ arq worker（consumer）──▶ 跑 agent
        │                                                                           │
        └────────── 立即返回 job_id ◀──── GET /jobs/{id} 查结果 ◀──────────────────┘

    直接收原始 dict，而不是 IssueRequest 这种带校验的模型.

二、
    对比 /issues/secure 用的是 req: IssueRequest——那是 Pydantic 模型，
    会自动校验字段（issue 必须非空、类型对不对等），不合法直接 422 拒绝.

    请求进来 → 当场构建 agent、调 LLM、等它跑完 → 把结果塞进响应返回。
    HTTP 连接会一直挂着，直到 agent 整个跑完才断开。
    问题：如果 agent 要跑 1 分钟，客户端就得等 1 分钟，而且这期间 web server 的并发连接、连接池、超时都被占着。

    
/jobs 这里故意用裸 dict，原因是一个权衡：

/issues（同步）	严格校验	反正要等它跑完，先校验省得白跑
/jobs（异步）	不校验	尽快入队、尽快释放 HTTP 连接，把校验成本推迟到 worker 真正执行时（甚至不校验，靠 worker 里 payload["text"] 的 KeyError 兜底）
核心思想是：异步方案的第一目标是「快」——让 web 层秒回、把重活丢给后台，所以入口处故意不做重校验、拿到数据就入队。这样 web 服务的高并发能力更强，不会被慢任务拖垮。
"""


@router.post("")
@limiter.limit("10/minute") # 带 X-API-Key → 按 tenant:租户id 分桶，每个租户每分钟 10 次；
async def submit_async(req_body: dict, request: Request):
    """提交到 arq 队列，立即返回 job_id。"""
    from tasks.queue import enqueue_issue

    payload = {
        "channel": req_body.get("channel", "api"),
        "user_id": req_body.get("user_id", "anonymous"),
        "text": req_body["text"],
        "conversation_id": req_body.get("conversation_id", uuid4().hex),
    }

    raw = f"{payload['channel']}|{payload['user_id']}|{payload['text']}"
    _job_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    job_id = await enqueue_issue(request.app.state.arq_redis, payload, _job_id) # 只入队，不执行
    return {"job_id": job_id, "status": "queued"}  # 立刻返回


@router.get("/{job_id}")
async def query_job(job_id: str, request: Request):
    from tasks.queue import get_job_result
    return await get_job_result(request.app.state.arq_redis, job_id)