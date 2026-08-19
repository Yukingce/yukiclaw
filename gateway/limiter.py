"""gateway/limiter.py —— SlowAPI 限流器（按 tenant_id 分桶 + Redis 多副本共享）"""
from slowapi import Limiter
from slowapi.util import get_remote_address

from infra.settings import get_settings


def _tenant_key(request) -> str:
    """限流维度：把 API key 映射到租户,按 tenant_id 分桶(复用第 9 章映射)。"""
    api_key = request.headers.get("X-API-Key")
    tenant = get_settings().api_keys.get(api_key) if api_key else None
    return f"tenant:{tenant}" if tenant else get_remote_address(request)

# Limiter 利用 key_func 算出桶标识 → 去 Redis（storage_uri 配的）里对那个 key 做原子计数
# 超了就抛 RateLimitExceeded
# 计数存在 Redis：所以限流是跨进程/跨副本共享的，不是单进程内存计数。多实例部署时也能正确限制。

limiter = Limiter(
    key_func=_tenant_key,
    storage_uri=get_settings().redis_url,   # Redis：多副本共享计数
    default_limits=[],                      # 按路由单独设
)