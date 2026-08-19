"""
    通用 Webhook 接入（含飞书 challenge 验证）

    两个工程要点：
    ① 回调验证：飞书 Webhook 模式首次会发 {"type":"url_verification","challenge":"..."}，
    必须原样返回 challenge（官方要求 1 秒内）；
    ② 快速返回 + 幂等：外部平台会重试，需立即 200 并对重复事件去重（用事件 id）。
"""
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse

from channels.base import InboundMessage
from channels.handler import handle_message
from infra.logging import get_logger
from infra.idempotency import seen_before
from obs.metrics import IDEMPOTENCY_HITS

logger = get_logger()

router = APIRouter(prefix="/webhook", tags=["webhook"])

# # 简易幂等：记录已处理事件 id（生产用 Redis 带 TTL）
# _seen_event_ids: set[str] = set()


@router.post("/feishu")
async def feishu_webhook(request: Request, background: BackgroundTasks):
    """
    飞书 Webhook 回调模式入口（与 长连接 feishu_ws.py 二选一）。事件回调 需要公网ip
    这个函数用不到。

    background.add_task(_process)          # 丢给后台，不 await
    return JSONResponse({"code": 0})       # 立刻返回 200，不等 agent
    它把真正跑 agent 的 _process 塞进 BackgroundTasks，然后马上返回 200。飞书（调用方）几毫秒就拿到确认，
    agent 在响应发出之后才在后台慢慢跑，结果再用 fs.send 主动推回飞书。
    """
    body = await request.json()

    # ① challenge 验证：配置回调地址时飞书会先发这个，原样返回 challenge
    if body.get("type") == "url_verification":
        return JSONResponse({"challenge": body.get("challenge", "")})

    # # ② 幂等去重：用事件 id，避免飞书重试导致重复处理
    # header = body.get("header", {})
    # event_id = header.get("event_id")
    # if event_id and event_id in _seen_event_ids:
    #     return JSONResponse({"code": 0})   # 重复事件，直接确认
    # if event_id:
    #     _seen_event_ids.add(event_id)

    # ② 幂等去重:用 Redis(跨副本共享、带 TTL、重启不丢、原子无竞态)
    header = body.get("header", {})
    event_id = header.get("event_id")
    if event_id and await seen_before(request.app.state.redis, event_id, ttl_seconds=3600):
        IDEMPOTENCY_HITS.inc()        # 埋点：重复事件被挡
        return JSONResponse({"code": 0})  # 重复事件,直接确认

    # ③ 解析消息事件 → 归一化（仅处理 im.message.receive_v1 文本）
    if header.get("event_type") == "im.message.receive_v1":
        import json as _json
        message = body.get("event", {}).get("message", {})
        text = ""
        try:
            text = _json.loads(message.get("content", "{}")).get("text", "").strip()
        except Exception:
            pass
        chat_id = message.get("chat_id", "")
        open_id = body.get("event", {}).get("sender", {}).get("sender_id", {}).get("open_id", "unknown")

        if text:
            inbound = InboundMessage(
                channel="feishu", user_id=open_id, text=text,
                conversation_id=chat_id, raw=body,
            )
            cp = request.app.state.checkpointer
            st = request.app.state.store

            async def _process():
                from channels.feishu import FeishuChannel
                fs = FeishuChannel()
                reply = await handle_message(inbound, cp, st)
                await fs.send(chat_id, reply)

            # 用 FastAPI 原生 BackgroundTasks（响应返回后执行）——比游离的
            # asyncio.create_task 正规：它由框架管理、保证在响应后调度。
            # ⚠️ 但它仍无持久化/重试/重启不丢——慢任务的可靠处理 队列。
            background.add_task(_process)

    # ④ 立即 200（飞书 3 秒约束）——不等 agent 跑完
    return JSONResponse({"code": 0})


@router.post("/generic")
async def generic_webhook(request: Request):
    """通用 Webhook：任意外部系统 POST {"text": "...", "source": "..."} 触发 DevMate。

    注意：本端点是【同步等待】agent 跑完才返回的——适合"任务较快"或调用方
    能接受长等待的场景（如脚本/CI）。一个跑几分钟的 Issue 会让调用方一直阻塞、
    甚至超时。

    handle_message(...) 就是调 agent 去处理消息（可能要跑模型、调工具、写数据库）。
    这里用 await 直接等它执行完，拿到 reply 之后才 return。也就是说：HTTP 请求的响应里就带着 agent 的最终回答，
    但代价是请求方必须一直等着 agent 跑完。

    让它"提交即返回、后台异步跑、结果可查"是任务队列的事。
    Web 前端目前调的就是这个端点，所以慢任务时网页会一直转圈。
    """
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"code": 1, "msg": "empty text"})

    inbound = InboundMessage(
        channel="webhook", user_id=body.get("source", "external"),
        text=text, conversation_id=body.get("conversation_id", "default"), raw=body,
    )
    reply = await handle_message(
        inbound, request.app.state.checkpointer, request.app.state.store
    )
    return JSONResponse({"code": 0, "reply": reply})