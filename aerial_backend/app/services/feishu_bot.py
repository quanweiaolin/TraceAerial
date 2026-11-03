"""飞书LLM问答机器人的长连接接入层。

跟services/notifier.py里那个"只能单向发消息"的webhook机器人是两个完全
不同的东西，不要混用：这里是一个新建的企业自建应用(App ID/Secret，见
app/config.py的feishu_llm_app_id/feishu_llm_app_secret)，用长连接
(WebSocket)接收群/私聊消息事件，不需要公网HTTPS地址。

实现上有个绕不开的约束：lark_oapi.ws.Client.start()是阻塞调用，内部
自己管一个事件循环(模块级`loop = asyncio.get_event_loop()`，在第一次
import这个子模块的线程里就定下来了，之后一直用这一个)，跟FastAPI/uvicorn
自己那个正在跑的主事件循环不是一回事，不能在同一个线程里跑(start()会
一直占着跑到进程退出，而且它内部run_until_complete的loop如果恰好是
主loop，在主loop已经在跑的情况下会直接报错)。所以这里专门开一个daemon
线程跑这个长连接客户端——`import lark_oapi`延迟到这个新线程内部第一次
执行，让它绑定的是这个新线程自己的loop，不会跟uvicorn主loop抢。

真正要用到数据库的部分(调用agent查数据库、发送回复)不能留在这个新线程
的loop里跑——SQLAlchemy的async引擎/asyncpg连接池绑在创建它们那个loop上
(主loop)，跨loop用会出错。所以消息到达时用
asyncio.run_coroutine_threadsafe()把实际处理逻辑弹回主loop执行(主loop
的引用由main.py的lifespan()在应用启动时用asyncio.get_running_loop()
拿到，传给这个模块的start())。

触发条件(2026-11-02先按这个简化版本上线，不是跟用户讨论过的正式决定，
真用起来觉得不对可以再调)：私聊(p2p)每条消息都回复，因为私聊本来就是
在单独跟这个机器人对话；群聊里要求消息里至少有一个@，否则不回复(避免
群里聊别的事情也被刷屏)——这里只检查"有没有@"，不检查"@的是不是这个
机器人本身"，因为后者需要先调一次接口拿机器人自己的open_id，这个阶段
先从简。
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
from typing import Any

from app.config import settings
from app.llm_agent.runner import ask

_AT_TAG_RE = re.compile(r"<at[^>]*>.*?</at>")


def start(main_loop: asyncio.AbstractEventLoop) -> None:
    """main.py的lifespan()里调用，开一个daemon线程跑长连接客户端。
    没配置App ID/Secret就什么都不做(方便学生本地跑的时候不用管这个)。"""
    if not settings.feishu_llm_app_id or not settings.feishu_llm_app_secret:
        print("[feishu_bot] 未配置LLM_Feishu_AppId/AppSecret，跳过长连接接入", flush=True)
        return

    thread = threading.Thread(target=_run_ws_client, args=(main_loop,), daemon=True)
    thread.start()
    print("[feishu_bot] 长连接客户端线程已启动", flush=True)


def _run_ws_client(main_loop: asyncio.AbstractEventLoop) -> None:
    import lark_oapi as lark  # 延迟到这个新线程里才第一次import，见模块docstring

    def on_message_receive(event: Any) -> None:
        _dispatch_message(main_loop, event)

    def _ignore(event: Any) -> None:
        pass  # 群里消息被加/取消表情回应时飞书也会推这个事件，我们不关心，注册个空处理器
              # 不然SDK自己的dispatcher找不到处理器会报"processor not found"刷日志

    handler = (
        lark.EventDispatcherHandler.builder("", "")  # 长连接模式不需要encrypt_key/verification_token
        .register_p2_im_message_receive_v1(on_message_receive)
        .register_p2_im_message_reaction_created_v1(_ignore)
        .register_p2_im_message_reaction_deleted_v1(_ignore)
        .build()
    )
    client = lark.ws.Client(
        settings.feishu_llm_app_id,
        settings.feishu_llm_app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )
    client.start()  # 阻塞，一直跑到进程退出


def _dispatch_message(main_loop: asyncio.AbstractEventLoop, event: Any) -> None:
    """长连接线程自己的loop里同步调用，只做轻量过滤+文本提取，不碰数据库。"""
    message = event.event.message
    sender = event.event.sender

    if sender.sender_type != "user":
        return  # 不回应其它机器人/系统消息，避免互相触发
    if message.message_type != "text":
        return
    if message.chat_type != "p2p" and not message.mentions:
        return  # 群聊里没人@就不回复，避免刷屏；私聊每条都处理

    try:
        text = json.loads(message.content).get("text", "")
    except (TypeError, ValueError):
        return
    text = _AT_TAG_RE.sub("", text).strip()
    if not text:
        return

    print(f"[feishu_bot] 收到消息: chat_id={message.chat_id} text={text!r}", flush=True)
    asyncio.run_coroutine_threadsafe(
        _handle_message(message.chat_id, message.message_id, text), main_loop
    )


async def _handle_message(chat_id: str, message_id: str, question: str) -> None:
    """跳回主loop执行：真正调用agent查数据库+把回复发回飞书。"""
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

    try:
        answer = await ask(question)
    except Exception as e:
        print(f"[feishu_bot] agent处理消息失败: {e}", flush=True)
        answer = f"处理这个问题时出错了：{e}"

    client = (
        lark.Client.builder()
        .app_id(settings.feishu_llm_app_id)
        .app_secret(settings.feishu_llm_app_secret)
        .build()
    )
    request = (
        ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            ReplyMessageRequestBody.builder()
            .content(json.dumps({"text": answer}, ensure_ascii=False))
            .msg_type("text")
            .build()
        )
        .build()
    )
    resp = await client.im.v1.message.areply(request)
    if resp.success():
        print(f"[feishu_bot] 已回复: chat_id={chat_id} answer={answer!r}", flush=True)
    else:
        print(f"[feishu_bot] 回复消息失败: chat_id={chat_id} code={resp.code} msg={resp.msg}", flush=True)
