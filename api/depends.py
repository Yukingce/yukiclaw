"""
    依赖注入：从 app.state 取共享资源

    路由通过 Depends 拿到 checkpointer/store，而不是直接访问全局，
    便于测试时替换、也让依赖关系显式。

    HTTP 请求进来
  → FastAPI 创建当前请求的 Request 对象
  → 发现路由参数 checkpointer=Depends(get_checkpointer)
  → 检查 get_checkpointer 签名，看到 request: Request（内置类型）
  → 自动把当前 Request 传进去，调用 get_checkpointer(request)
  → 拿到返回值 request.app.state.checkpointer
  → 作为 checkpointer 实参，再调用 submit_issue(...)
"""
from fastapi import Request


def get_checkpointer(request: Request):
    """取在 lifespan 里建好的 AsyncPostgresSaver。"""
    return request.app.state.checkpointer


def get_store(request: Request):
    """取在 lifespan 里建好的 AsyncPostgresStore。"""
    return request.app.state.store