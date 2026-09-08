"""
飞书通知服务。

设计成"队列消费者"模式而不是"事件发生时同步调用"：
  - 各处业务逻辑(扫盘/校验失败/阶段完成)只管往 notifications 表插一条 pending 记录
  - 一个独立的后台worker(process_pending_notifications)定时轮询、批量发送
优点：飞书接口抖动/限流不会拖慢主业务请求；失败了下一轮还会重试。
"""
import base64
import hashlib
import hmac
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Notification, Video

MAX_RETRY = 5


def _format_message(n: Notification) -> str:
    """把不同event_type格式化成人能看懂的飞书文本，需要更花哨可以换成飞书卡片消息。"""
    templates = {
        "new_task": "🆕 新视频入库，请及时认领处理：{file_path}",
        "stage_failed": "❌ 阶段处理失败：视频 {video} 在阶段 {stage} 出错：{error}",
        "validation_escalation": "⚠️ {username} 连续 {count} 次数据提交校验失败，请协助确认",
        "qa_batch_ready": "🔍 新一批检测QA待标注，共 {count} 个视频，请及时处理",
    }
    payload = n.payload or {}
    template = templates.get(n.event_type, "{payload}")
    try:
        return template.format(**payload, payload=payload)
    except KeyError:
        return f"[{n.event_type}] {payload}"


def _gen_sign(timestamp: int, secret: str) -> str:
    """飞书自定义机器人签名算法：用"timestamp\\n密钥"当HMAC-SHA256的key，
    对空内容求HMAC，结果再base64。跟飞书官方文档给的算法一致，不是我们自己发明的。
    参考: https://open.feishu.cn/document/ukTMukTMukTM/ucTM5YjL3ETO24yNxkjN
    """
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


async def send_feishu_message(text: str) -> bool:
    if not settings.feishu_webhook_url:
        return False
    payload: dict = {"msg_type": "text", "content": {"text": text}}
    if settings.feishu_webhook_secret:
        # 机器人开了签名校验时必须带timestamp+sign，否则飞书直接拒收(sign match fail)
        timestamp = int(time.time())
        payload["timestamp"] = str(timestamp)
        payload["sign"] = _gen_sign(timestamp, settings.feishu_webhook_secret)
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(settings.feishu_webhook_url, json=payload)
        return resp.status_code == 200


def _mark_sent_or_retry(n: Notification, ok: bool) -> None:
    if ok:
        n.status = "sent"
        n.sent_at = datetime.now(timezone.utc)
    else:
        n.retry_count += 1
        if n.retry_count >= MAX_RETRY:
            n.status = "failed"


async def process_pending_notifications(session: AsyncSession, batch_size: int = 50) -> int:
    """由定时任务(比如每分钟)调用一次，返回本次处理的条数。

    new_task这类通知按"视频所在的直接父文件夹"分组合并成一条消息发送，
    避免一次扫盘发现一堆新视频时逐条刷屏；其他event_type照常逐条发送。
    """
    result = await session.execute(
        select(Notification)
        .where(Notification.status == "pending")
        .where(Notification.retry_count < MAX_RETRY)
        .limit(batch_size)
    )
    pending = result.scalars().all()

    new_task_notifications = [n for n in pending if n.event_type == "new_task"]
    other_notifications = [n for n in pending if n.event_type != "new_task"]

    groups: dict[str, list[Notification]] = defaultdict(list)
    for n in new_task_notifications:
        file_path = (n.payload or {}).get("file_path", "")
        folder = str(Path(file_path).parent) if file_path else "(未知路径)"
        groups[folder].append(n)

    for folder, group in groups.items():
        text = f"🆕 新视频入库，请及时认领处理\n📁 {folder}\n共 {len(group)} 个视频"
        ok = await send_feishu_message(text)
        for n in group:
            _mark_sent_or_retry(n, ok)

    for n in other_notifications:
        text = _format_message(n)
        ok = await send_feishu_message(text)
        _mark_sent_or_retry(n, ok)

    await session.commit()
    return len(pending)


async def send_daily_summary(session: AsyncSession) -> None:
    """每日汇总，由定时任务(比如每天早上)调用一次。目前统计4项：
      - 今日新增视频数(含真新视频和重新编码产生的新记录，即下面第4项的父集)
      - 其中时长<10s的短视频数(duration_seconds为空的不算，ffprobe没读出来的另算)
      - 其中缺经纬度的数量(collect_lon/collect_lat任意一个为空就算，代表没有
        SRT文件或者SRT解析失败，任意一个缺了都算)
      - 其中"重新编码更新"的数量(replaces_sha256不为空，即长期瘦身转码任务
        产生的记录，不是真新视频；这类不发即时通知，只在这里汇总)
    以后要加新指标，在这个函数里加一个 func.count().filter(...) 列即可，
    不用改调用方(app/main.py的定时任务)。
    """
    today_start = func.date_trunc("day", func.now())

    new_count = func.count().filter(Video.created_at >= today_start)
    short_count = func.count().filter(
        Video.created_at >= today_start,
        Video.duration_seconds.is_not(None),
        Video.duration_seconds < 10,
    )
    no_gps_count = func.count().filter(
        Video.created_at >= today_start,
        or_(Video.collect_lon.is_(None), Video.collect_lat.is_(None)),
    )
    re_encoded_count = func.count().filter(
        Video.created_at >= today_start,
        Video.replaces_sha256.is_not(None),
    )

    result = await session.execute(
        select(new_count, short_count, no_gps_count, re_encoded_count)
    )
    new_total, short_total, no_gps_total, re_encoded_total = result.one()

    lines = ["📊 每日视频汇总", f"今日新增视频: {new_total} 个"]
    if re_encoded_total:
        lines.append(f"🔄 其中重新编码更新(瘦身转码): {re_encoded_total} 个")
    if short_total:
        lines.append(f"⚠️ 其中时长<10s的短视频: {short_total} 个")
    if no_gps_total:
        lines.append(f"⚠️ 其中缺经纬度(疑似无SRT/解析失败): {no_gps_total} 个")
    await send_feishu_message("\n".join(lines))


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
