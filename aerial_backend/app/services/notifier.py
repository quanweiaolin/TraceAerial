"""
飞书通知服务。

设计成"队列消费者"模式而不是"事件发生时同步调用"：
  - 各处业务逻辑(扫盘/校验失败/阶段完成)只管往 notifications 表插一条 pending 记录
  - 一个独立的后台worker(process_pending_notifications)定时轮询、批量发送
优点：飞书接口抖动/限流不会拖慢主业务请求；失败了下一轮还会重试。
"""
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Notification

MAX_RETRY = 5


def _format_message(n: Notification) -> str:
    """把不同event_type格式化成人能看懂的飞书文本，需要更花哨可以换成飞书卡片消息。"""
    templates = {
        "new_task": "🆕 新视频入库，请及时认领处理：{file_path}",
        "stage_failed": "❌ 阶段处理失败：视频 {video} 在阶段 {stage} 出错：{error}",
        "validation_escalation": "⚠️ {username} 连续 {count} 次数据提交校验失败，请协助确认",
    }
    payload = n.payload or {}
    template = templates.get(n.event_type, "{payload}")
    try:
        return template.format(**payload, payload=payload)
    except KeyError:
        return f"[{n.event_type}] {payload}"


async def send_feishu_message(text: str) -> bool:
    if not settings.feishu_webhook_url:
        return False
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            settings.feishu_webhook_url,
            json={"msg_type": "text", "content": {"text": text}},
        )
        return resp.status_code == 200


async def process_pending_notifications(session: AsyncSession, batch_size: int = 50) -> int:
    """由定时任务(比如每分钟)调用一次，返回本次处理的条数。"""
    result = await session.execute(
        select(Notification)
        .where(Notification.status == "pending")
        .where(Notification.retry_count < MAX_RETRY)
        .limit(batch_size)
    )
    pending = result.scalars().all()

    for n in pending:
        text = _format_message(n)
        ok = await send_feishu_message(text)
        if ok:
            n.status = "sent"
            n.sent_at = datetime.now(timezone.utc)
        else:
            n.retry_count += 1
            if n.retry_count >= MAX_RETRY:
                n.status = "failed"

    await session.commit()
    return len(pending)


async def enqueue_validation_escalation(
    session: AsyncSession, username: str, fail_count: int
) -> None:
    """校验服务发现某人连续失败超阈值时调用，通知 ziyu/quanwei 等负责人。"""
    for target in settings.escalation_usernames:
        session.add(
            Notification(
                event_type="validation_escalation",
                channel="feishu",
                payload={"username": username, "count": fail_count, "target": target},
            )
        )
    await session.commit()
