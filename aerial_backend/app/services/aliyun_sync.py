"""
阿里云PDS自动下载流程。计划由APScheduler每6小时调一次 run_aliyun_scan。

云端目录结构：飞手上传时只负责建"飞行会话文件夹"(命名规范见
SESSION_FOLDER_RE，例如 ChangChun_PanShiRoad_Mavic3E_200m_wangziyu_20260912)，
这个文件夹深度不固定(可能直接在某个空间根目录下，也可能被套了别的组织性
目录)，所以遍历时不按固定层级找，而是在整棵目录树里按"文件夹名字是不是
符合会话命名规范"来identify——哪一层先出现这个命名规范，哪一层就是要监控
的最小单位，不用再继续往下找(M3E系列这个文件夹下面还会套一层DJI_xxx才是
真正装MP4/SRT的目录，Mini/Air系列这个文件夹本身就直接装文件，两种都要把
这个文件夹下全部文件(不管嵌套多深)当成一个整体收集)。
"年份_城市_AerialVideos_季度"这个分类文件夹不是云端已有的东西，是本地落盘
时我们自己从会话文件夹名里解析出城市+日期算出来的，拼在本地路径最外层，
不要求云端真的存在这层。

流程：
  1. 遍历PDS每个可访问的drive：文件夹名符合会话命名规范 → 收集为一个监控
     单位；文件夹名"像"会话命名但没完全对上(比如设备型号拼错/不在白名单)
     → 直接拒绝(报警，不再往下探索)；完全不沾边的文件夹(比如还没整理的
     组织性目录) → 继续往下探索子目录
  2. 每个监控单位跟aliyun_folder_scans里它自己最新一行比大小/文件数(递归
     统计，不只是直接子文件)，写一条新的心跳记录(变了才存完整文件列表)
  3. 还没判定完成(aliyun_sync_events里没有它的folder_ready事件)的，连续N次
     心跳都没变化(N=settings.aliyun_stability_scans_required)就判定"上传
     完成"：检查磁盘空间→下载到settings.aliyun_download_dir/<算出来的年份_
     城市_AerialVideos_季度>/<会话文件夹名>/<相对路径>(生产环境download_dir
     直接配成NAS真实路径，不再经过SSD暂存)→全部文件下载成功后，对这个
     本地目录调用scanner.run_scan扫描入videos库→写folder_ready事件
     (下载/扫描任一步失败都算没完成，不单独标记，下次心跳自动整体重试)
  4. 已经判定完成的文件夹继续被监控：
     - 又有新文件/变大 → 飞书提示(不是错误，继续下载新增部分) +
       写issue事件(reupload_after_complete)
     - 变小/文件不见了 → 对比上一次有完整文件列表的心跳，找出具体丢了哪个
       文件 → 飞书提示 + 写issue事件(file_deleted)

不做的事(明确暂缓，见项目里的讨论记录)：
  - unknown_uploader检测(飞手归属要靠按人分文件夹的真实结构，测试数据还没有)
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AliyunFolderScan, AliyunSyncEvent
from app.services.notifier import send_feishu_message
from app.services.scanner import run_scan

REPO_ROOT = Path(__file__).resolve().parents[3]
PDS_TOOL_ROOT = REPO_ROOT / "extern" / "pds-drive-sync"
if str(PDS_TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(PDS_TOOL_ROOT))

from pds_sync.core import (  # noqa: E402  (必须在sys.path插入之后才能import)
    ITEM_PROJECTION,
    PdsCli,
    PdsError,
    _conflict_path,
    _file_matches,
    _paginate,
    list_drives,
)


def _cli_binary_path() -> Path:
    configured = settings.aliyun_cli_binary
    if configured:
        return Path(configured)
    return PDS_TOOL_ROOT / ".tools" / "aliyun"


def _build_cli() -> PdsCli:
    """给list-file/list-drive这类快调用用，超时给默认的60s，网络/API有问题
    能尽快失败，不用等太久。"""
    return PdsCli(_cli_binary_path())


def _build_download_cli() -> PdsCli:
    """专门给download-to-local用，航测视频单个文件常常几GB，60s的默认超时
    根本不够，下载还没传完就被判定超时(见2026-09-25的飞书报警：
    'Aliyun CLI timed out after 60s')。用settings.aliyun_download_timeout_seconds
    单独给一个大得多的超时，不影响其他快调用的"快速失败"特性。"""
    return PdsCli(_cli_binary_path(), timeout_seconds=settings.aliyun_download_timeout_seconds)


# 飞行会话文件夹命名规范：City_Site_Drone_Height_Person_Date，
# 例如 ChangChun_PanShiRoad_Mavic3E_200m_wangziyu_20260912。
#   City   采集地点城市，拼音，每个字首字母大写，如 BeiJing
#   Site   地点名，拼音，每个单词首字母大写，如 PanShiRoad
#   Drone  设备型号，固定白名单(SESSION_FOLDER_DEVICES)，不在名单里的一律
#          拒绝——宁可先报错拦下来，也不要静默收了一个拼错/没见过的机型名
#   Height 采集高度，数字+小写m，如 200m
#   Person 采集人员姓名拼音，全小写，如 wangziyu
#   Date   上传日期 YYYYMMDD
SESSION_FOLDER_DEVICES = ("Mavic3E", "Air2S", "Air3S", "Mini4Pro", "Mini3Pro")
SESSION_FOLDER_RE = re.compile(
    r"^(?P<city>[A-Z][a-zA-Z]*)_(?P<site>[A-Z][a-zA-Z]*)_"
    r"(?P<drone>" + "|".join(SESSION_FOLDER_DEVICES) + r")_"
    r"(?P<height>\d+)m_(?P<person>[a-z]+)_(?P<date>\d{8})$"
)


_SESSION_FOLDER_DEVICES_LOWER = {device.lower() for device in SESSION_FOLDER_DEVICES}


def _diagnose_session_folder_name(name: str) -> list[str]:
    """给命名不规范的候选文件夹做字段级别的诊断，返回具体哪几个字段哪里错了
    (而不是笼统地说"不符合规范")，报警/log都用这个，方便人一眼定位要改哪里。"""
    parts = name.split("_")
    if len(parts) != 6:
        return [f"应该是 City_Site_Drone_Height_Person_Date 共6段(用_分隔)，实际是{len(parts)}段"]
    city, site, drone, height, person, date = parts
    reasons = []
    if not re.fullmatch(r"[A-Z][a-zA-Z]*", city):
        reasons.append(f"City字段{city!r}应为拼音且首字母大写，如BeiJing")
    if not re.fullmatch(r"[A-Z][a-zA-Z]*", site):
        reasons.append(f"Site字段{site!r}应为拼音且每个单词首字母大写，如PanShiRoad")
    if drone not in SESSION_FOLDER_DEVICES:
        devices = "/".join(SESSION_FOLDER_DEVICES)
        reasons.append(f"Drone字段{drone!r}不在设备白名单里，只认{devices}")
    if not re.fullmatch(r"\d+m", height):
        reasons.append(f"Height字段{height!r}应为数字+小写m，如200m")
    if not re.fullmatch(r"[a-z]+", person):
        reasons.append(f"Person字段{person!r}应全部小写，如wangziyu")
    if not re.fullmatch(r"\d{8}", date):
        reasons.append(f"Date字段{date!r}应为8位数字YYYYMMDD，如20260912")
    return reasons or ["跟规范整体对不上，但逐字段检查没发现具体问题，可能是分隔符本身有问题"]


def _looks_like_session_candidate(name: str) -> bool:
    """名字里有没有出现过设备型号白名单里的词——用来判断这个文件夹"像"是在
    想当会话文件夹、只是没完全写对(该报错拦下来)，还是纯粹的组织性目录
    (该继续往下探索)。按"_"分段做整段匹配，不是子串匹配，避免误伤；大小写
    不敏感——"Mini3pro"这种大小写写错的也该被拦下来报错，而不是因为没有
    精确匹配上白名单就被当成无关目录静默放过。"""
    return any(segment.lower() in _SESSION_FOLDER_DEVICES_LOWER for segment in name.split("_"))


def _derive_category_folder(match: re.Match) -> str:
    """从会话文件夹名解析出的城市+日期，算出本地落盘要用的分类文件夹名
    (年份_城市_AerialVideos_季度)。这一层云端不存在，是我们本地自己拼的。"""
    date = match.group("date")
    year = date[:4]
    quarter = (int(date[4:6]) - 1) // 3 + 1
    return f"{year}_{match.group('city')}_AerialVideos_Q{quarter}"


def free_space_gb(path: str | Path) -> float:
    """path所在文件系统的剩余空间(GiB)。path本身不需要存在，会往上找最近的
    存在的祖先目录来查(比如下载目标目录还没建出来的情况)。"""
    p = Path(path)
    while not p.exists():
        if p.parent == p:
            raise FileNotFoundError(f"找不到任何存在的祖先目录: {path}")
        p = p.parent
    usage = shutil.disk_usage(p)
    return usage.free / (1024 ** 3)


async def ensure_enough_space(path: str | Path, min_free_gb: float | None = None) -> bool:
    """下载前检查目标路径所在盘的剩余空间够不够，不够就发飞书通知并返回False。"""
    threshold = min_free_gb if min_free_gb is not None else settings.aliyun_min_free_gb
    free_gb = free_space_gb(path)
    if free_gb >= threshold:
        return True
    print(f"[aliyun] 磁盘空间不足：{path} 剩余 {free_gb:.1f} GiB，低于阈值 {threshold:.0f} GiB", flush=True)
    await send_feishu_message(
        f"⚠️ 磁盘空间不足，暂停阿里云下载：{path} 所在盘剩余 {free_gb:.1f} GiB，"
        f"低于阈值 {threshold:.0f} GiB"
    )
    return False


def _file_entry(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_id": str(item.get("file_id") or ""),
        "name": item.get("name"),
        "relative_path": item.get("relative_path"),
        "size_bytes": int(item.get("size") or 0),
        "content_hash": item.get("content_hash"),
        "content_hash_algo": item.get("content_hash_name"),
        "created_at": item.get("created_at"),
    }


def _list_children(cli: PdsCli, drive_id: str, parent_id: str) -> list[dict[str, Any]]:
    return _paginate(
        cli,
        [
            "list-file",
            "--drive-id", drive_id,
            "--parent-file-id", parent_id,
            "--limit", "100",
            "--cli-query", ITEM_PROJECTION,
        ],
    )


def _gather_all_files(cli: PdsCli, drive_id: str, start_file_id: str) -> list[dict[str, Any]]:
    """递归收集一个文件夹下全部文件(不管嵌套多深，比如M3E系列会话文件夹下面
    还套了一层DJI_xxx)，每个文件附带relative_path(相对这个起始文件夹的相对
    路径，POSIX斜杠分隔)，用来还原本地落盘时的目录结构。"""
    collected: list[dict[str, Any]] = []
    queue: deque[tuple[str, str]] = deque([(start_file_id, "")])
    while queue:
        parent_id, prefix = queue.popleft()
        for item in _list_children(cli, drive_id, parent_id):
            name = item.get("name") or ""
            relative = f"{prefix}/{name}" if prefix else name
            if item.get("type") == "folder":
                queue.append((str(item.get("file_id") or ""), relative))
            else:
                item = dict(item)
                item["relative_path"] = relative
                collected.append(item)
    return collected


def _walk_session_folders(
    cli: PdsCli, drive: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """遍历一个drive，找出全部"飞行会话文件夹"(命名符合SESSION_FOLDER_RE)。
    深度不固定——不管这个文件夹在目录树里多深，只要名字对上了就当成一个
    监控单位，不用再继续往下找；名字不对但看着像(命中设备型号白名单)的，
    当成命名错误拒绝，不再往下探索；完全不沾边的目录继续往下探索子目录。
    是同步阻塞调用(内部是subprocess)，调用方要丢进线程池。
    返回 (合法会话列表, 命名错误候选列表)。"""
    drive_id = str(drive.get("drive_id") or "")
    queue: deque[tuple[str, str]] = deque([("root", "/")])
    visited = {"root"}
    sessions: list[dict[str, Any]] = []
    invalid_candidates: list[dict[str, Any]] = []
    while queue:
        parent_id, parent_path = queue.popleft()
        for item in _list_children(cli, drive_id, parent_id):
            if item.get("type") != "folder":
                continue  # 散装文件(不属于任何会话文件夹)忽略，跟之前行为一致
            file_id = str(item.get("file_id") or "")
            if not file_id or file_id in visited:
                continue
            visited.add(file_id)
            name = item.get("name") or ""
            folder_path = f"{parent_path.rstrip('/')}/{name}"
            match = SESSION_FOLDER_RE.match(name)
            if match:
                raw_files = _gather_all_files(cli, drive_id, file_id)
                sessions.append({
                    "drive_id": drive_id,
                    "cloud_folder_id": file_id,
                    "cloud_folder_path": folder_path,
                    "category_folder": _derive_category_folder(match),
                    "session_name": name,
                    "raw_files": raw_files,
                })
            elif _looks_like_session_candidate(name):
                invalid_candidates.append({
                    "drive_id": drive_id,
                    "cloud_folder_id": file_id,
                    "cloud_folder_path": folder_path,
                    "name": name,
                })
            else:
                queue.append((file_id, folder_path))
    return sessions, invalid_candidates


async def _latest_scan(session: AsyncSession, cloud_folder_id: str) -> AliyunFolderScan | None:
    result = await session.execute(
        select(AliyunFolderScan)
        .where(AliyunFolderScan.cloud_folder_id == cloud_folder_id)
        .order_by(AliyunFolderScan.scanned_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _recent_scans(session: AsyncSession, cloud_folder_id: str, limit: int) -> list[AliyunFolderScan]:
    result = await session.execute(
        select(AliyunFolderScan)
        .where(AliyunFolderScan.cloud_folder_id == cloud_folder_id)
        .order_by(AliyunFolderScan.scanned_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def _latest_ready_event(session: AsyncSession, cloud_folder_id: str) -> AliyunSyncEvent | None:
    result = await session.execute(
        select(AliyunSyncEvent)
        .where(AliyunSyncEvent.cloud_folder_id == cloud_folder_id)
        .where(AliyunSyncEvent.event_type == "folder_ready")
        .order_by(AliyunSyncEvent.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _latest_issue_event(
    session: AsyncSession, cloud_folder_id: str, issue_type: str
) -> AliyunSyncEvent | None:
    result = await session.execute(
        select(AliyunSyncEvent)
        .where(AliyunSyncEvent.cloud_folder_id == cloud_folder_id)
        .where(AliyunSyncEvent.event_type == "issue")
        .where(AliyunSyncEvent.issue_type == issue_type)
        .order_by(AliyunSyncEvent.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _last_known_files(session: AsyncSession, cloud_folder_id: str) -> list[dict[str, Any]] | None:
    """上一次changed=True的心跳里存的完整文件列表，用于跟这次的文件列表diff。"""
    result = await session.execute(
        select(AliyunFolderScan)
        .where(AliyunFolderScan.cloud_folder_id == cloud_folder_id)
        .where(AliyunFolderScan.changed.is_(True))
        .order_by(AliyunFolderScan.scanned_at.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    return row.files if row is not None else None


def _download_one_file(
    cli: PdsCli, local_relative_path: str, raw_item: dict[str, Any], download_root: Path
) -> dict[str, Any]:
    """同步阻塞调用(内部是subprocess)，调用方要丢进线程池。

    不用pds_sync.core.download_item：它会把本地路径按云端路径1:1镜像，但
    现在本地落盘结构是我们自己拼的(分类文件夹是本地算出来的，云端不存在)，
    local_relative_path由调用方(_download_folder_files)算好传进来，不依赖
    cloud_path/resolve-path API。哈希跳过(_file_matches)/冲突改名
    (_conflict_path)逻辑跟download_item一致，直接复用pds_sync.core里现成
    的纯函数。"""
    drive_id = str(raw_item.get("drive_id") or "")
    file_id = str(raw_item.get("file_id") or "")
    root = Path(download_root).resolve()
    target = (root / local_relative_path).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"本地相对路径逃出了下载目录: {local_relative_path}")
    target.parent.mkdir(parents=True, exist_ok=True)

    status = "downloaded"
    if target.exists():
        if target.is_file() and _file_matches(raw_item, target):
            return {"file_id": file_id, "local_path": str(target), "status": "skipped"}
        target = _conflict_path(target, file_id)
        status = "conflicted"

    temporary = target.with_name(f".{target.name}.{secrets.token_hex(6)}.part")
    try:
        result = cli.pds([
            "download-to-local",
            "--drive-id", drive_id,
            "--file-id", file_id,
            "--save-to", str(temporary),
        ])
        if not temporary.is_file():
            raise PdsError(f"download did not create a local file for {file_id}")
        local_size = temporary.stat().st_size
        expected_sizes = {
            int(value)
            for value in (raw_item.get("size"), result.get("size") if isinstance(result, dict) else None)
            if isinstance(value, int) and value >= 0
        }
        if expected_sizes and (len(expected_sizes) != 1 or local_size not in expected_sizes):
            raise PdsError(f"download size verification failed for {file_id}")
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        return {
            "file_id": file_id, "local_path": str(target),
            "size": local_size, "status": status,
        }
    finally:
        if temporary.exists():
            temporary.unlink()


async def _download_folder_files(folder: dict[str, Any], cli: PdsCli) -> list[dict[str, Any]]:
    """下载文件夹里全部当前文件(含嵌套子目录里的)；已经下载过的会按哈希跳过
    (_download_one_file内部逻辑)，所以对"完成后又有新增"这种情况重新调一次
    也是安全的、只会真正下载新文件。

    本地落盘路径 = download_root/category_folder/session_name/相对路径，
    category_folder是本地算出来的"年份_城市_AerialVideos_季度"，不是云端
    真实存在的目录。

    注意：不直接用传进来的cli(那个是给list-file这类快调用用的，超时只有
    60s)——航测视频单个文件常常几GB，60s传不完就会被判超时(见2026-09-25
    实际报警：'Aliyun CLI timed out after 60s')，这里单独用
    _build_download_cli()构造一个大超时的实例。"""
    folder_path = folder["cloud_folder_path"]
    download_cli = _build_download_cli()
    downloaded: list[dict[str, Any]] = []
    for raw_item in folder["raw_files"]:
        relative_path = raw_item.get("relative_path") or raw_item.get("name")
        local_relative = f"{folder['category_folder']}/{folder['session_name']}/{relative_path}"
        cloud_path = f"{folder_path.rstrip('/')}/{relative_path}"
        try:
            result = await asyncio.to_thread(
                _download_one_file, download_cli, local_relative, raw_item, Path(settings.aliyun_download_dir)
            )
        except (PdsError, OSError, ValueError) as exc:
            await send_feishu_message(f"⚠️ 阿里云下载失败：{folder_path} 里的 {relative_path}：{exc}")
            downloaded.append({
                "cloud_file_id": raw_item.get("file_id"),
                "cloud_path": cloud_path,
                "content_hash": raw_item.get("content_hash"),
                "content_hash_algo": raw_item.get("content_hash_name"),
                "local_path": None,
                "download_status": "failed",
                "error": str(exc),
            })
            continue
        downloaded.append({
            "cloud_file_id": result.get("file_id"),
            "cloud_path": cloud_path,
            "content_hash": raw_item.get("content_hash"),
            "content_hash_algo": raw_item.get("content_hash_name"),
            "local_path": result.get("local_path"),
            "download_status": result.get("status"),  # downloaded/skipped/conflicted
        })
    return downloaded


def _log_download_detail(context: str, folder_path: str, downloaded: list[dict[str, Any]]) -> None:
    """log里给"这次下载具体怎么样"用的详细版本，比飞书消息内容更全——每个
    文件的下载结果(状态/本地路径/失败原因)都列出来。只在真的触发下载动作
    时打印一次(不是每次心跳都打)，不会刷屏。"""
    print(f"[aliyun] {context}：{folder_path}", flush=True)
    for d in downloaded:
        name = d.get("cloud_path") or d.get("cloud_file_id")
        status = d.get("download_status")
        if status == "failed":
            detail = f"error={d.get('error')}"
        else:
            detail = f"-> {d.get('local_path')}"
        print(f"[aliyun]   {status:<10} {name} {detail}", flush=True)


def _download_summary_line(downloaded: list[dict[str, Any]]) -> str:
    """按download_item实际返回的status分类计数，跟"总共尝试了几个文件"分开说，
    避免"已下载N个"这种表述在N个其实都是已存在跳过时显得有误导。"""
    new_count = sum(1 for d in downloaded if d.get("download_status") == "downloaded")
    skipped_count = sum(1 for d in downloaded if d.get("download_status") == "skipped")
    conflicted_count = sum(1 for d in downloaded if d.get("download_status") == "conflicted")
    failed_count = sum(1 for d in downloaded if d.get("download_status") == "failed")
    return (
        f"新下载 {new_count} 个，已存在跳过 {skipped_count} 个，冲突 {conflicted_count} 个，"
        f"失败 {failed_count} 个，共 {len(downloaded)} 个文件"
    )


async def _report_invalid_session_name(session: AsyncSession, candidate: dict[str, Any]) -> None:
    """文件夹名命中了设备型号白名单、但整体格式没对上SESSION_FOLDER_RE——
    只在第一次检测到时报警+记一条issue事件，避免同一个问题每6小时刷一次
    飞书；人工把目录名改对之后，下次心跳会重新判定，不需要额外的"解除标记"
    操作。这类文件夹不会被收集文件/不会进入心跳监控，是终态的拒绝。"""
    cloud_folder_id = candidate["cloud_folder_id"]
    folder_path = candidate["cloud_folder_path"]
    already_flagged = await _latest_issue_event(session, cloud_folder_id, "invalid_session_name")
    if already_flagged is not None:
        return
    # 逐字段诊断，精确到"哪个字段、写的是什么、应该长什么样"，不是笼统的
    # "不符合规范"——人一看报警就知道要去云端哪个文件夹改哪个字段
    field_reasons = _diagnose_session_folder_name(candidate["name"])
    reason = "；".join(field_reasons)
    session.add(AliyunSyncEvent(
        event_type="issue",
        issue_type="invalid_session_name",
        cloud_folder_id=cloud_folder_id,
        cloud_folder_path=folder_path,
        videos=[],
        notified_at=datetime.now(timezone.utc),
    ))
    print(f"[aliyun] 命名不符合规范，拒绝下载：{folder_path}\n  {reason}", flush=True)
    await send_feishu_message(f"⚠️ 文件夹命名不符合规范，已跳过下载：{folder_path}\n{reason}")


async def _trigger_download(
    session: AsyncSession,
    cli: PdsCli,
    folder: dict[str, Any],
    file_entries: list[dict[str, Any]],
    scan_completed_at: datetime,
) -> None:
    folder_path = folder["cloud_folder_path"]

    if not await ensure_enough_space(settings.aliyun_download_dir):
        return  # 磁盘不够，飞书已经发过了，等下次心跳再试

    download_started_at = datetime.now(timezone.utc)
    downloaded = await _download_folder_files(folder, cli)
    download_finished_at = datetime.now(timezone.utc)
    all_succeeded = all(d.get("download_status") != "failed" for d in downloaded)
    _log_download_detail("文件夹判定上传完成，开始下载", folder_path, downloaded)

    scanned_in = False
    if all_succeeded:
        # 全部文件下载成功后，才把这个文件夹扫描进videos库——跟scanner.py的
        # 常规定时扫盘复用同一套逻辑(哈希去重/瘦身检测/飞书通知都一样)，
        # 只是触发时机和范围精确到"刚下载完的这一个文件夹"，不用等下一次
        # 全量扫盘。scan失败也算这次没完成，不标记download_finished_at，
        # 下次心跳(云端内容没变、还在"稳定"状态)会自动整体重试
        local_folder = Path(settings.aliyun_download_dir).resolve() / folder["category_folder"] / folder["session_name"]
        try:
            await run_scan(session, str(local_folder))
            scanned_in = True
        except Exception as exc:  # noqa: BLE001  范围特意放宽：扫描入库这一步绝不能让整个扫描循环中断
            print(f"[aliyun] 下载完成但扫描入库失败：{local_folder}  {exc}", flush=True)
            await send_feishu_message(f"⚠️ 阿里云下载完成但扫描入库失败：{folder_path}\n{exc}")
    all_succeeded = all_succeeded and scanned_in

    session.add(AliyunSyncEvent(
        event_type="folder_ready",
        cloud_folder_id=folder["cloud_folder_id"],
        cloud_folder_path=folder_path,
        videos=downloaded,
        scan_completed_at=scan_completed_at,
        download_started_at=download_started_at,
        # 只有"全部文件下载成功 + 扫描入库成功"才标记download_finished_at，
        # 这样_latest_ready_event判断"已完成"才会准确——只要有一步失败，
        # 这里就留空，下次心跳(云端内容没变、还是"稳定"状态)会自动重新
        # 触发一次完整的流程，已成功的部分(下载/扫描)靠各自的幂等逻辑跳过，
        # 只会真正重试失败的那部分
        download_finished_at=download_finished_at if all_succeeded else None,
        notified_at=datetime.now(timezone.utc),
    ))
    if all_succeeded:
        await send_feishu_message(
            f"✅ 阿里云文件夹下载完成并已扫描入库：{folder_path}\n{_download_summary_line(downloaded)}"
        )
    else:
        await send_feishu_message(
            f"⚠️ 阿里云文件夹处理未全部成功，下次心跳会自动重试：{folder_path}\n{_download_summary_line(downloaded)}"
        )


async def _handle_reupload(
    session: AsyncSession, cli: PdsCli, folder: dict[str, Any], file_entries: list[dict[str, Any]]
) -> None:
    folder_path = folder["cloud_folder_path"]
    if not await ensure_enough_space(settings.aliyun_download_dir):
        return  # 磁盘不够，飞书已经发过了，等下次心跳再试

    download_started_at = datetime.now(timezone.utc)
    downloaded = await _download_folder_files(folder, cli)
    download_finished_at = datetime.now(timezone.utc)
    _log_download_detail("完成后又有新上传，继续下载", folder_path, downloaded)

    session.add(AliyunSyncEvent(
        event_type="issue",
        issue_type="reupload_after_complete",
        cloud_folder_id=folder["cloud_folder_id"],
        cloud_folder_path=folder_path,
        videos=downloaded,
        download_started_at=download_started_at,
        download_finished_at=download_finished_at,
        notified_at=datetime.now(timezone.utc),
    ))
    await send_feishu_message(
        f"⚠️ 已判定完成的文件夹又有新上传：{folder_path}\n{_download_summary_line(downloaded)}"
    )


async def _handle_possible_deletion(
    session: AsyncSession,
    folder: dict[str, Any],
    file_entries: list[dict[str, Any]],
) -> None:
    folder_path = folder["cloud_folder_path"]
    last_files = await _last_known_files(session, folder["cloud_folder_id"])
    missing_names = []
    if last_files is not None:
        current_ids = {f["file_id"] for f in file_entries}
        missing_names = [f["name"] for f in last_files if f["file_id"] not in current_ids]

    session.add(AliyunSyncEvent(
        event_type="issue",
        issue_type="file_deleted",
        cloud_folder_id=folder["cloud_folder_id"],
        cloud_folder_path=folder_path,
        videos=[{"name": name} for name in missing_names],
        notified_at=datetime.now(timezone.utc),
    ))
    detail = "、".join(missing_names) if missing_names else "(具体文件名对比不出来，可能是历史记录不足)"
    print(f"[aliyun] 完成后检测到文件被删除：{folder_path}，疑似丢失：{detail}", flush=True)
    await send_feishu_message(
        f"⚠️ 已判定完成的文件夹里有文件不见了：{folder_path}，疑似被删除：{detail}"
    )


async def _process_folder(session: AsyncSession, cli: PdsCli, folder: dict[str, Any]) -> None:
    file_entries = [_file_entry(item) for item in folder["raw_files"]]
    size_bytes = sum(e["size_bytes"] for e in file_entries)
    file_count = len(file_entries)

    previous = await _latest_scan(session, folder["cloud_folder_id"])
    changed = previous is None or previous.size_bytes != size_bytes or previous.file_count != file_count

    scan = AliyunFolderScan(
        drive_id=folder["drive_id"],
        cloud_folder_id=folder["cloud_folder_id"],
        cloud_folder_path=folder["cloud_folder_path"],
        size_bytes=size_bytes,
        file_count=file_count,
        changed=changed,
        files=file_entries if changed else None,
    )
    session.add(scan)
    await session.flush()  # 拿到scan.scanned_at

    ready_event = await _latest_ready_event(session, folder["cloud_folder_id"])
    already_downloaded = ready_event is not None and ready_event.download_finished_at is not None

    if not already_downloaded:
        recent = await _recent_scans(
            session, folder["cloud_folder_id"], settings.aliyun_stability_scans_required
        )
        stable = (
            len(recent) >= settings.aliyun_stability_scans_required
            and all(r.size_bytes == size_bytes and r.file_count == file_count for r in recent)
        )
        if stable:
            await _trigger_download(session, cli, folder, file_entries, scan.scanned_at)
        return

    if not changed:
        return
    if file_count > previous.file_count or size_bytes > previous.size_bytes:
        await _handle_reupload(session, cli, folder, file_entries)
    else:
        await _handle_possible_deletion(session, folder, file_entries)


async def run_aliyun_scan(session: AsyncSession) -> None:
    """完整跑一次：遍历全部drive，找出全部飞行会话文件夹，逐个记心跳、判断
    是否要下载/报警；命名错误的候选文件夹单独报警，不进入监控。

    每个文件夹处理完就commit一次，不是等全部文件夹跑完才一次性commit——
    单个文件夹的下载可能要跑很久(大文件)，中途被Ctrl-C/进程被杀的话，
    只在最后commit会导致这一整轮扫描、包括之前已经处理完的其他文件夹的
    心跳记录，全部跟着白跑(2026-09-25发现aliyun_sync_events是空的就是
    因为这个：中途Ctrl-C，从来没走到过最后那次commit)。"""
    cli = _build_cli()
    drives = await asyncio.to_thread(list_drives, cli, settings.aliyun_spaces)
    folder_count = 0
    for drive in drives:
        sessions, invalid_candidates = await asyncio.to_thread(_walk_session_folders, cli, drive)
        for candidate in invalid_candidates:
            await _report_invalid_session_name(session, candidate)
            await session.commit()
        for folder in sessions:
            await _process_folder(session, cli, folder)
            await session.commit()
            folder_count += 1
    print(f"[aliyun] 扫描完成: drive_count={len(drives)} folder_count={folder_count}", flush=True)
