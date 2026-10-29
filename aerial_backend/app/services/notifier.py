"""
飞书通知服务，兼跨程序的Postgres NOTIFY推送。

设计成"队列消费者"模式而不是"事件发生时同步调用"：
  - 各处业务逻辑(扫盘/校验失败/阶段完成)只管往 notifications 表插一条 pending 记录
  - 一个独立的后台worker(process_pending_notifications)定时轮询、批量发送
优点：飞书接口抖动/限流不会拖慢主业务请求；失败了下一轮还会重试。

------------------------------------------------------------------
【跨程序集成】其他连同一个Postgres实例的程序(比如拿Claude API跑整图
environment标注那个)，不用轮询，在自己的连接上执行一次
`LISTEN aerial_new_folder_video_names;`，就能实时收到新视频的推送。粒度是
文件夹：payload是JSON字符串，形如
{"folder": "<文件夹绝对路径>", "video_names": ["a.MP4", "b.MP4", ...]}。
跟飞书new_task走的是同一套"静默期"debounce(见NOTIFY_DEBOUNCE_SECONDS)——
不是扫盘一发现新视频就立刻推送，而是等这个文件夹静默够10分钟(大概率
已经扫完/上传完了，不会再有更多文件陆续进来)才一次性推送这期间新增的
文件名，避免像阿里云下载那边的"稳定期"判断一样太快反应导致推送太频繁
(2026-10-22达成一致：类比阿里云同步的stability_scans等待机制)。同一个
文件夹以后又有新视频静默期满了，会再推一条新的payload，只含那次新增的
文件名，不是历史累计的全部。单个文件夹一次性到几百个视频很常见(实测见过
279个)，payload超过Postgres 8000字节上限时会自动按video_names切成多条
NOTIFY(folder相同)，不会报错(见_chunk_video_names)。
------------------------------------------------------------------
"""
import base64
import hashlib
import hmac
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Notification, Video

MAX_RETRY = 5
PREFIX_SCAN = "【数据库扫描】"  # 2026-10-28起，数据库扫描(scanner.py)相关的飞书
# 消息统一加这个前缀，方便一眼看出是哪个组件发的；以后新增组件照这个惯例来
SUPPRESSED_EVENT_TYPES = {"validation_escalation", "qa_batch_ready"}  # 2026-10-28
# 决定这两类完全不发(飞书+log都不要)——数据提交校验升级、QA待标注批次这两个
# 场景目前不需要主动通知，直接标记sent、不留任何痕迹(见当天讨论)
NOTIFY_DEBOUNCE_SECONDS = 600  # 同一文件夹最后一条pending通知的created_at距今不足10分钟，
# 说明这个文件夹大概率还在被扫描任务陆续发现新文件(大文件夹会被扫盘按
# COMMIT_BATCH_SIZE拆成好几个批次)，先不发，等这个文件夹静默够10分钟
# 再合并成一条消息——避免一个大文件夹被拆成好几条"共50个视频"连续刷屏
# (2026-10-16发现：一个265个视频的文件夹被拆成6条外观雷同的消息)。
# 跨程序的pg_notify推送复用同一个debounce窗口，道理一样：等这批大概率
# 已经传完了再通知下游去跑Claude API，不要它还在陆续上传就频繁触发。
NEW_VIDEO_NOTIFY_CHANNEL = "aerial_new_folder_video_names"  # 跨程序LISTEN用的channel名，见上面模块docstring
NOTIFY_PAYLOAD_MAX_BYTES = 7800  # 留点余量，Postgres NOTIFY payload硬上限是8000字节


def _chunk_video_names(folder: str, names: list[str], max_bytes: int = NOTIFY_PAYLOAD_MAX_BYTES) -> list[list[str]]:
    """把一个文件夹里新增的视频文件名列表切成若干块，保证每块序列化成
    {"folder": folder, "video_names": [...]}这个JSON之后不超过max_bytes字节——
    单个文件夹一次性到几百个视频很常见(实测见过279个)，不切的话payload肯定
    超过Postgres NOTIFY的8000字节硬上限，直接报错。"""
    chunks: list[list[str]] = []
    current: list[str] = []
    for name in names:
        candidate = current + [name]
        payload_size = len(json.dumps({"folder": folder, "video_names": candidate}).encode("utf-8"))
        if current and payload_size > max_bytes:
            chunks.append(current)
            current = [name]
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _format_message(n: Notification) -> str:
    """把不同event_type格式化成人能看懂的飞书文本，需要更花哨可以换成飞书卡片消息。"""
    templates = {
        "new_task": "🆕 新视频入库，请及时认领处理：{file_path}",
        "stage_failed": "❌ 阶段处理失败：视频 {video} 在阶段 {stage} 出错：{error}",
        "validation_escalation": "⚠️ {username} 连续 {count} 次数据提交校验失败，请协助确认",
        "qa_batch_ready": "🔍 新一批检测QA待标注，共 {count} 个视频，请及时处理",
        "folder_moved": f"{PREFIX_SCAN} 📦 文件夹被整体移动，请知悉\n原位置：{{old_folder}}\n新位置：{{new_folder}}\n共 {{file_count}} 个视频",
        "folder_duplicate": f"{PREFIX_SCAN} ⚠️ 同一批视频在两个位置都真实存在，疑似重复下载/拷贝，请确认后手动清理\n位置A：{{old_folder}}\n位置B：{{new_folder}}\n共 {{file_count}} 个视频",
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


async def _process_grouped_video_notifications(
    session: AsyncSession,
    event_type: str,
    build_message,
    *,
    feishu_enabled: bool,
    emit_cross_program_notify: bool,
) -> int:
    """new_task/re_encoded共用的"按文件夹分组+静默期debounce"逻辑：扫盘按
    COMMIT_BATCH_SIZE分批提交，一个大文件夹一次扫描会产生好几批pending
    通知，这里统一等这个文件夹静默够NOTIFY_DEBOUNCE_SECONDS(大概率已经
    扫完了，不会再有同一文件夹的新通知陆续进来)才合并成一条消息发出去，
    不按每次提交各发一条——new_task这边2026-10-16就因为没做这个处理，
    一个265个视频的文件夹被拆成6条外观雷同的消息刷屏；re_encoded(瘦身
    转码)同样的风险：2026-09-29发现过一晚上202个瘦身更新。

    feishu_enabled=False时只打日志、不真的调用飞书(用于暂时关闭某个
    event_type的飞书推送，但还想保留处理记录，不堆积成pending)。
    emit_cross_program_notify=True时，debounce满足的同时顺带给跨程序的
    LISTEN订阅者推一次(见模块docstring"跨程序集成")——目前只有new_task
    需要，re_encoded是替换旧视频，不是下游Claude API标注关心的"新环境"。
    """
    result = await session.execute(
        select(Notification)
        .where(Notification.status == "pending")
        .where(Notification.retry_count < MAX_RETRY)
        .where(Notification.event_type == event_type)
    )
    notifications = result.scalars().all()

    groups: dict[str, list[Notification]] = defaultdict(list)
    for n in notifications:
        file_path = (n.payload or {}).get("file_path", "")
        folder = str(Path(file_path).parent) if file_path else "(未知路径)"
        groups[folder].append(n)

    now = datetime.now(timezone.utc)
    sent_count = 0
    for folder, group in groups.items():
        latest_created = max(n.created_at for n in group)
        if (now - latest_created).total_seconds() < NOTIFY_DEBOUNCE_SECONDS:
            continue
        message_text = build_message(folder, group)
        if feishu_enabled:
            ok = await send_feishu_message(message_text)
        else:
            print(f"[notify] (飞书已暂停，仅记日志) {message_text}", flush=True)
            ok = True
        for n in group:
            _mark_sent_or_retry(n, ok)
        sent_count += len(group)

        if emit_cross_program_notify:
            video_names = [
                n.payload.get("original_name")
                for n in group
                if n.payload and n.payload.get("original_name")
            ]
            if video_names:
                for chunk in _chunk_video_names(folder, video_names):
                    await session.execute(
                        text("SELECT pg_notify(:channel, :payload)"),
                        {
                            "channel": NEW_VIDEO_NOTIFY_CHANNEL,
                            "payload": json.dumps({"folder": folder, "video_names": chunk}),
                        },
                    )
    return sent_count


async def process_pending_notifications(session: AsyncSession, batch_size: int = 50) -> int:
    """由定时任务(比如每分钟)调用一次，返回本次处理的条数。

    new_task/re_encoded这两类按"视频所在的直接父文件夹"分组+静默期debounce
    合并成一条消息发送(见_process_grouped_video_notifications)，避免一次
    扫盘发现一堆视频时逐条刷屏；其他event_type照常逐条发送，其中
    SUPPRESSED_EVENT_TYPES这两类(校验升级/QA待标注批次)2026-10-28起完全
    不发，飞书和log都没有，只标记sent。
    """
    other_result = await session.execute(
        select(Notification)
        .where(Notification.status == "pending")
        .where(Notification.retry_count < MAX_RETRY)
        .where(Notification.event_type.not_in(["new_task", "re_encoded"]))
        .limit(batch_size)
    )
    other_notifications = other_result.scalars().all()
    for n in other_notifications:
        if n.event_type in SUPPRESSED_EVENT_TYPES:
            _mark_sent_or_retry(n, True)
            continue
        message_text = _format_message(n)
        ok = await send_feishu_message(message_text)
        _mark_sent_or_retry(n, ok)

    sent_new_task_count = await _process_grouped_video_notifications(
        session,
        "new_task",
        lambda folder, group: f"{PREFIX_SCAN} 🆕 新视频入库，请及时认领处理\n📁 {folder}\n共 {len(group)} 个视频",
        # 2026-10-28暂停：粒度太细(按10分钟静默期分组，不是按"地点真的传完
        # 了")、跟aliyun那边的完成判定也只是弱相关，不该被下游标注团队当成
        # 开工信号用，见当天讨论——等"地点级完成通知"设计出来再决定要不要恢复
        feishu_enabled=False,
        emit_cross_program_notify=True,
    )
    sent_re_encoded_count = await _process_grouped_video_notifications(
        session,
        "re_encoded",
        lambda folder, group: f"{PREFIX_SCAN} 🔄 检测到瘦身转码(重新编码)\n📁 {folder}\n共 {len(group)} 个视频",
        feishu_enabled=True,  # 2026-10-28新加，之前只在每日汇总里统计，现在额外发即时消息
        emit_cross_program_notify=False,
    )

    await session.commit()
    return len(other_notifications) + sent_new_task_count + sent_re_encoded_count


async def send_daily_summary(session: AsyncSession) -> None:
    """每日汇总，由定时任务(每天早上9点)调用一次，统计的是**昨天一整天**
    (昨天0点到今天0点，左闭右开)，不是"今天0点到现在"。

    原来统计口径是"今天0点以后"，会产生一个每天约15小时的盲区：早上9点跑
    的时候，当天9点到午夜之间产生的记录还没发生，那天永远不会被任何一次
    汇总报到——下一天9点那次统计口径又变成"从下一天0点开始"，早就把前一
    天9点后的记录甩在窗口之外。2026-09-29发现的"瘦身更新只报了1个，实际
    当晚11点多有202个"就是这个盲区导致的，数据库里其实都在，只是没被任何
    一次飞书消息报出来。改成固定统计"昨天一整天"，跟每天9点跑一次的节奏
    首尾相接，不会再有盲区。

    目前统计4项：
      - 昨日新增视频数(含真新视频和重新编码产生的新记录，即下面第4项的父集)
      - 其中时长<10s的短视频数(duration_seconds为空的不算，ffprobe没读出来的另算)
      - 其中缺经纬度的数量(collect_lon/collect_lat任意一个为空就算，代表没有
        SRT文件或者SRT解析失败，任意一个缺了都算)
      - 其中"重新编码更新"的数量(replaces_sha256不为空，即长期瘦身转码任务
        产生的记录，不是真新视频；这类不发即时通知，只在这里汇总)
    以后要加新指标，在这个函数里加一个 func.count().filter(...) 列即可，
    不用改调用方(app/main.py的定时任务)。
    """
    today_start = func.date_trunc("day", func.now())
    yesterday_start = today_start - text("interval '1 day'")

    def _yesterday(*extra):
        return func.count().filter(
            Video.created_at >= yesterday_start,
            Video.created_at < today_start,
            *extra,
        )

    new_count = _yesterday()
    short_count = _yesterday(
        Video.duration_seconds.is_not(None),
        Video.duration_seconds < 10,
    )
    no_gps_count = _yesterday(
        or_(Video.collect_lon.is_(None), Video.collect_lat.is_(None)),
    )
    re_encoded_count = _yesterday(Video.replaces_sha256.is_not(None))

    result = await session.execute(
        select(new_count, short_count, no_gps_count, re_encoded_count)
    )
    new_total, short_total, no_gps_total, re_encoded_total = result.one()

    lines = ["📊 每日视频汇总(昨日)", f"昨日新增视频: {new_total} 个"]
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
