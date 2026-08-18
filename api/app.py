"""api/app.py —— FastAPI 入口 + lifespan（建池/持久化/挂资源）

工程化核心：
- 在 lifespan 建一个 AsyncConnectionPool，同时构造 AsyncPostgresSaver + AsyncPostgresStore；
- setup() 建表（首次）；把 checkpointer/store/redis 挂到 app.state 供全局复用；
- 关闭时统一释放——资源生命周期完全归服务所有，绝不在请求里临时建连。
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres import AsyncPostgresStore

from infra.redis import build_redis
from infra.persistence import open_persistence
from infra.logging import get_logger
from profiles import register_all_profiles
from api.schemas import HealthResponse
from api.routes.issues import router as issues_router#在下面有

from channels.webhook import router as webhook_router
from fastapi.middleware.cors import CORSMiddleware

logger = get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 注册 Harness Profile（第 7 章）——在任何 agent 构建前
    register_all_profiles()

    # 建并打开 Postgres 连接池（一个池，喂 saver + store）
    # 用同一个池构造异步 saver / store，并 setup（首次建表）
    pool, checkpointer, store = await open_persistence()

    # Redis 池
    redis = build_redis()

    # 挂到 app.state，供所有请求复用，Starlette 提供的「应用级共享储物柜」，整个进程里每个请求都能访问到它。
    app.state.pg_pool = pool
    app.state.checkpointer = checkpointer
    app.state.store = store
    app.state.redis = redis

    try:
        yield                      # 服务运行中
    finally:
        # ⑥ 关闭时统一释放
        await redis.aclose()
        await pool.close()
        logger.info("连接池已释放")


app = FastAPI(title="YuKiClaw DevMate API", lifespan=lifespan)
app.include_router(issues_router)
app.include_router(webhook_router)   # 注册后才有 /webhook/feishu 和 /webhook/generic

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