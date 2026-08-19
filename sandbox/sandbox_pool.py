"""
    隔离优先的沙箱池（面向 BaseSandbox 接口）

    - 并发上限：信号量保证同时存在的沙箱不超过 max_size；
    - 隔离优先：每任务拿干净沙箱，release 时销毁（用完即弃），不串数据；
    - 交回可用上下文 (backend, workdir)，不是裸 sandbox；
    - async with acquire() 保证异常也归还/销毁,不泄漏。
"""
import asyncio
from contextlib import asynccontextmanager

from infra.settings import get_settings
from infra.logging import get_logger
from sandbox.docker_manager import create_one_sandbox, seed_project, destroy_sandbox

logger = get_logger()


class SandboxPool:
    def __init__(self, max_size: int):
        self._sem = asyncio.Semaphore(max_size)   # 并发限流，创建一个异步信号量，最多允许 max_size 个协程同时进入某段代码
        self._max = max_size

    @asynccontextmanager
    async def acquire(self):
        """借一个干净沙箱；用完销毁（隔离优先）。yield (backend, workdir)。"""

        """
        当前协程申请一个通行证。
            如果还有通行证，立即拿到，继续执行。
            如果已经有 max_size 个协程拿走了通行证，就会在这里异步等待。
            等别的协程执行 self._sem.release() 归还通行证后，它才能继续
        """
        await self._sem.acquire()
        sandbox = None
        try:
            sandbox = await create_one_sandbox()   # 全新、加固、隔离
            await seed_project(sandbox)            # 池内部 seed 好
            yield sandbox, sandbox.workdir         # ← 交回可用上下文
        finally:
            if sandbox is not None:
                await destroy_sandbox(sandbox)     # 用完即弃：销毁,不复用、不串数据
            self._sem.release()


_pool: SandboxPool | None = None


def get_sandbox_pool() -> SandboxPool:
    global _pool
    if _pool is None:
        _pool = SandboxPool(max_size=get_settings().sandbox_pool_size)
    return _pool