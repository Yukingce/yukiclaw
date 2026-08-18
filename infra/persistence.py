"""
    建异步持久化资源（saver/store），供服务与独立进程共用

    之前把建池/saver/store 写在了 FastAPI lifespan 里。但飞书长连接接收端是
    独立进程、不经过 lifespan，需要自己建一份。于是把这段抽出来两边复用——
    这也修正了"独立进程 from api.app import app 拿不到 app.state"的错误接法。
"""
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres import AsyncPostgresStore

from infra.db import build_pg_pool
from infra.logging import get_logger
logger = get_logger()

async def open_persistence():
    """打开连接池并构造 saver/store（已 setup）。返回 (pool, checkpointer, store)。

    调用方负责在退出时 await pool.close()。
    """

    # 建并打开 Postgres 连接池（一个池，喂 saver + store）
    pool = build_pg_pool()
    await pool.open()
    logger.info("Postgres 连接池已打开")

    # 用同一个池构造异步 saver / store，并 setup（首次建表）
    checkpointer = AsyncPostgresSaver(pool)
    store = AsyncPostgresStore(pool)
    await checkpointer.setup()
    await store.setup()
    logger.info("AsyncPostgresSaver / AsyncPostgresStore 已 setup")
    
    return pool, checkpointer, store