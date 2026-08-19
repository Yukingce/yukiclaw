"""api/app.py —— FastAPI 入口 + lifespan（建池/持久化/挂资源）

工程化核心：
- 在 lifespan 建一个 AsyncConnectionPool，同时构造 AsyncPostgresSaver + AsyncPostgresStore；
- setup() 建表（首次）；把 checkpointer/store/redis 挂到 app.state 供全局复用；
- 关闭时统一释放——资源生命周期完全归服务所有，绝不在请求里临时建连。
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres import AsyncPostgresStore

from infra.redis import build_redis
from infra.persistence import open_persistence
from infra.logging import get_logger
from profiles import register_all_profiles
from api.schemas import HealthResponse

from api.routes.issues import router as issues_router

from api.routes.tasks_bg import router as tasks_bg_router
from tasks.store import init_task_table

from api.routes.jobs import router as jobs_router  
from tasks.queue import get_arq_redis             

from channels.webhook import router as webhook_router
from fastapi.middleware.cors import CORSMiddleware

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from gateway.limiter import limiter

from obs.metrics import RATELIMIT_REJECTED
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from obs.metrics import PrometheusMiddleware

logger = get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 注册 Harness Profile（第 7 章）——在任何 agent 构建前
    register_all_profiles()

    # 建并打开 Postgres 连接池（一个池，喂 saver + store）
    # 用同一个池构造异步 saver / store，并 setup（首次建表）
    pool, checkpointer, store = await open_persistence()

    await init_task_table(pool) # 长任务异步化，方案一，BackgroundTasks + PostgreSQL

    # Redis 池
    arq_redis = await get_arq_redis()   # 建 arq 入队用的 Redis 连接池

    # 挂到 app.state，供所有请求复用，Starlette 提供的「应用级共享储物柜」，整个进程里每个请求都能访问到它。
    app.state.pg_pool = pool
    app.state.checkpointer = checkpointer
    app.state.store = store
    app.state.arq_redis = arq_redis         # 挂到 state 供入队端点用

    try:
        yield                      # 服务运行中
    finally:
        # 关闭时统一释放
        await arq_redis.aclose()
        await pool.close()
        logger.info("连接池已释放")


app = FastAPI(title="YuKiClaw DevMate API", lifespan=lifespan)
app.include_router(issues_router)    # /issues/secure 和 /issues
app.include_router(webhook_router)   # /webhook/feishu 和 /webhook/generic
app.include_router(tasks_bg_router)  # /tasks 和 /tasks/{task_id}
app.include_router(jobs_router)      # /jobs 和 /jobs/{job_id}
app.add_middleware(PrometheusMiddleware)          # 装 HTTP 采集中间件

"""
注册限流超限时的异常处理器。

当某个请求触发了限流配额，SlowAPI 会抛 RateLimitExceeded 异常。
_rate_limit_exceeded_handler（从 slowapi 导入）是 SlowAPI 提供的标准处理器，
它会把这个异常转换成 HTTP 429 Too Many Requests 响应返回给客户端。
不加这一行，超限时异常会冒泡成 500；加了之后，客户端会收到语义正确的 429。
"""
async def rate_limit_exceeded_handler(request, exc):
    tenant = getattr(request.state, "tenant", "unknown")

    RATELIMIT_REJECTED.labels(tenant=tenant).inc()

    return await _rate_limit_exceeded_handler(request, exc)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)  # 超限自动 429

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# 统一异常处理：不把内部堆栈暴露给客户端
@app.exception_handler(Exception)
async def unhandled_exc_handler(request: Request, exc: Exception):
    logger.exception("未处理异常：{}", exc)
    return JSONResponse(status_code=500, content={"detail": "内部错误，请稍后重试"})


# 健康检查：liveness（纯探活）
@app.get("/healthz", response_model=HealthResponse)
async def healthz():
    return HealthResponse(status="ok")


# 健康检查：readiness（依赖就绪才 OK）
@app.get("/readyz", response_model=HealthResponse)
async def readyz(request: Request):
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        return JSONResponse(status_code=503, content={"status": "not_ready", "detail": "pool 未就绪"})
    return HealthResponse(status="ready")

@app.get("/metrics")                              # 普通路由
async def metrics():
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        # 生产多进程（Gunicorn 多 worker）：聚合所有 worker
        from prometheus_client import CollectorRegistry, multiprocess
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        data = generate_latest(registry)
    else:
        data = generate_latest()                  # 本地单进程：默认全局 registry
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)