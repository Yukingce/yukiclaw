"""
    飞书长连接接收端（独立进程运行）

    用 lark 长连接客户端监听 im.message.receive_v1
    收到消息 → 立即回执（满足 3 秒）→ 在事件循环里处理并把结果发回。

    慢任务的可靠后台处理（持久化/重试/重启不丢）
    本文件不使用游离的 asyncio.create_task 当"后台"——那不可靠。

"""
import asyncio
import json

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from channels.base import InboundMessage
from channels.feishu import FeishuChannel
from channels.handler import handle_message
from infra.persistence import open_persistence
from infra.settings import get_settings
from infra.logging import get_logger

logger = get_logger()


def extract_feishu_message(event: P2ImMessageReceiveV1) -> InboundMessage | None:
    """从飞书消息事件解析出归一化 InboundMessage（仅处理文本）。

    长连接版和 Webhook 版都用它解析——避免重复（DRY）。
    飞书事件结构是层层嵌套的：event.event.message 才是消息体、event.event.sender 是发送者。
    """
    msg = event.event.message      # 消息体（含 chat_id、content、message_type 等）
    sender = event.event.sender    # 发送者信息
    chat_id = msg.chat_id          # 会话标识 → 我们用它做 conversation_id（同会话续聊的关键）
    # open_id 是发送者在本应用下的唯一标识；做了空值兜底，拿不到就标 unknown
    open_id = sender.sender_id.open_id if sender and sender.sender_id else "unknown"
    text = ""
    try:
        # ⚠️ 关键易错点：飞书文本消息的 content 不是纯文本，而是 JSON 字符串：'{"text":"你好"}'
        # 所以要先 json.loads 再取 "text" 字段——直接当字符串用会拿到一串带引号的 JSON。
        text = json.loads(msg.content).get("text", "").strip()
    except Exception:
        pass                       # 非文本消息（图片/文件等）content 结构不同，本章只处理文本
    if not text:
        return None                # 空文本或非文本消息：忽略
    return InboundMessage(channel="feishu", user_id=open_id, text=text,
                          conversation_id=chat_id, raw=None)


async def run():
    """启动长连接，常驻接收。持久化资源在本进程内建一份、全程复用。"""
    s = get_settings()
    feishu = FeishuChannel()
    pool, checkpointer, store = await open_persistence()
    loop = asyncio.get_running_loop()

    # agent 正在处理接收的信息
    async def _handle(inbound: InboundMessage):
        await feishu.send(inbound.conversation_id, "收到，正在处理…")   # 先回执
        try:
            reply = await handle_message(inbound, checkpointer, store)
        except Exception as e:  # noqa: BLE001
            logger.exception("处理飞书消息失败：{}", e)
            reply = "处理出错了，请稍后再试。"
        await feishu.send(inbound.conversation_id, reply)

    def on_message(event: P2ImMessageReceiveV1) -> None:
        # SDK 回调是同步的，且运行在 SDK 自己的线程——用 run_coroutine_threadsafe
        # 把协程安全地丢回我们的事件循环执行（不是游离的 create_task）。
        inbound = extract_feishu_message(event)
        if inbound is None:
            return
        logger.info("飞书收到消息：chat={} user={}", inbound.conversation_id, inbound.user_id)
        asyncio.run_coroutine_threadsafe(_handle(inbound), loop)

    """
        builder("", "")：创建一个事件分发器构造器。两个参数分别是 encrypt_key 和 verification_token，
        这是事件订阅(Webhook)模式下校验请求用的。长连接(WebSocket)模式不需要这两个值，所以传空字符串。

        .register_p2_im_message_receive_v1(on_message)：注册事件类型 → 回调函数的映射。
        含义是「当收到 im.message.receive_v1（即用户给机器人发了消息）这个事件时，调用 on_message 这个函数」。
        这是飞书事件订阅里最核心的一种：接收消息。

        .build()：把上面的配置真正构造出一个 EventDispatcherHandler 对象。
    """
    handler = (
        lark.EventDispatcherHandler.builder("", "")   # 长连接模式 encrypt_key/token 可空
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )

    """
        长连接客户端（负责「建立连接、收发事件」）

        s.feishu_app_id：飞书应用的 App ID，用来标识「我是哪个应用」。

        s.feishu_app_secret.get_secret_value()：App Secret，用于向飞书换取 access_token 做鉴权。

        event_handler=handler：把第 1 步建好的分发器挂到客户端上——连接建立后，
        SDK 收到事件就会按 handler 里的规则路由到对应回调。
        
        log_level=...：SDK 的日志级别
    """
    cli = lark.ws.Client(
        s.feishu_app_id, s.feishu_app_secret.get_secret_value(),
        event_handler=handler, log_level=lark.LogLevel.INFO,
    )
    logger.info("飞书长连接启动…（确保飞书后台已选『长连接接收事件』并发布版本）")
    try:
        # cli.start() 是阻塞的；放到线程里跑，保持本事件循环活着以处理回调协程
        await asyncio.to_thread(cli.start)
    finally:
        await pool.close()


if __name__ == "__main__":
    import sys
    # Windows 默认 ProactorEventLoop 与 psycopg 异步不兼容，切到 SelectorEventLoop
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run())