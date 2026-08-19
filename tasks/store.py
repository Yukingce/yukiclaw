"""
    长任务异步化： 方案一
    任务状态表的读写（方案一：BackgroundTasks 用）

    一张极简表记录每个异步任务的状态/结果，让"提交即返回的任务"事后可查、重启不丢记录。
    用 已有的 Postgres 连接池（app.state.pg_pool），不引入新组件。
"""
import json
from datetime import datetime, timezone

# 建表 SQL（首次在 lifespan 里执行一次）
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS async_tasks (
    task_id     TEXT PRIMARY KEY,
    status      TEXT NOT NULL
                CHECK (status IN ('pending', 'running', 'done', 'error')),
    channel     TEXT,
    payload     JSONB,
    result      JSONB,
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


async def init_task_table(pool) -> None:
    """建表（lifespan 启动时调一次）。"""
    async with pool.connection() as conn:
        await conn.execute(CREATE_TABLE_SQL)


async def create_task(pool, task_id: str, channel: str, payload: dict) -> None:
    now = datetime.now(timezone.utc)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO async_tasks (task_id, status, channel, payload, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (task_id, "pending", channel, json.dumps(payload), now, now),
        ) # 支持 %s 参数占位符的异步 PostgreSQL 驱动


async def update_task(pool, task_id: str, *, status: str,
                      result: dict | None = None, error: str | None = None) -> None:
    now = datetime.now(timezone.utc)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE async_tasks SET status=%s, result=%s, error=%s, updated_at=%s WHERE task_id=%s",
            (status, json.dumps(result) if result else None, error, now, task_id),
        )


async def get_task(pool, task_id: str) -> dict | None:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT task_id, status, result, error FROM async_tasks WHERE task_id=%s", (task_id,)
        ) # 让游标 cur 执行这条 SQL，并把查询结果保存在这个游标里。取 task_id, status, result, error 这四个
        row = await cur.fetchone() # 取数据
    # 之前配了 row_factory=dict_row，所以 row 是 dict
    return row