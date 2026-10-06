"""
阿里云PDS自动下载流程。计划由APScheduler每6小时调一次 run_aliyun_scan。

流程：
  1. 遍历PDS每个可访问的drive，找出全部"叶子文件夹"(不含子文件夹、直接装着
     MP4/SRT等文件的目录) —— 这就是要监控的最小单位
  2. 每个叶子文件夹跟aliyun_folder_scans里它自己最新一行比大小/文件数，
     写一条新的心跳记录(变了才存完整文件列表，见add_aliyun_folder_scans.sql)
  3. 还没判定完成(aliyun_sync_events里没有它的folder_ready事件)的文件夹：
     检查是否连续N次心跳都没变化(N=settings.aliyun_stability_scans_required)，
     稳定了就先校验次顶层目录名是否符合"年份_城市_AerialVideos_季度"这个
     命名规范(不符合直接拒绝下载，见_validate_top_level_folder)→检查磁盘
     空间→下载到settings.aliyun_download_dir(生产环境直接配成NAS真实路径，
     不再经过SSD暂存)→全部文件下载成功后，对这个文件夹调用scanner.run_scan
     扫描入videos库→写folder_ready事件(scan失败也算没完成，不单独标记)
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
from pathlib import Path, PurePosixPath
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


# 次顶层文件夹(PDS根目录下第一层)命名规范：年份_城市_AerialVideos_季度，
# 例如 2026_Changchun_AerialVideos_Q1。不符合这个规范的文件夹，见
# _validate_top_level_folder，直接拒绝下载，不能走到"落盘"这一步。
TOP_LEVEL_FOLDER_RE = re.compile(r"^\d{4}_[A-Za-z]+_AerialVideos_Q[1-4]$")


def _top_level_folder_name(cloud_folder_path: str) -> str | None:
    """从完整云端路径里取出根目录下第一层(次顶层)文件夹名。
    cloud_folder_path形如"/2026_Changchun_AerialVideos_Q1/xxx/DJI_xxx"，
    parts[0]是"/"本身，parts[1]才是次顶层文件夹。"""
    parts = PurePosixPath(cloud_folder_path).parts
    return parts[1] if len(parts) > 1 else None


def _validate_top_level_folder(cloud_folder_path: str) -> bool:
    name = _top_level_folder_name(cloud_folder_path)
    return name is not None and TOP_LEVEL_FOLDER_RE.match(name) is not None


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
        "size_bytes": int(item.get("size") or 0),
        "content_hash": item.get("content_hash"),
        "content_hash_algo": item.get("content_hash_name"),
        "created_at": item.get("created_at"),
    }


def _walk_leaf_folders(cli: PdsCli, drive: dict[str, Any]) -> list[dict[str, Any]]:
    """BFS遍历一个drive，找出全部"叶子文件夹"(不含子文件夹、直接装文件的目录)。
    是同步阻塞调用(内部是subprocess)，调用方要丢进线程池。"""
    drive_id = str(drive.get("drive_id") or "")
    queue: deque[tuple[str, str]] = deque([("root", "/")])
    visited = {"root"}
    leaf_folders: list[dict[str, Any]] = []
    while queue:
        parent_id, parent_path = queue.popleft()
        children = _paginate(
            cli,
            [
                "list-file",
                "--drive-id", drive_id,
                "--parent-file-id", parent_id,
                "--limit", "100",
                "--cli-query", ITEM_PROJECTION,
            ],
        )
        subfolders = [c for c in children if c.get("type") == "folder"]
        files = [c for c in children if c.get("type") == "file"]
        for folder in subfolders:
            file_id = str(folder.get("file_id") or "")
            if file_id and file_id not in visited:
                visited.add(file_id)
                child_path = f"{parent_path.rstrip('/')}/{folder.get('name')}"
                queue.append((file_id, child_path))
        if files and not subfolders:
            leaf_folders.append({
                "drive_id": drive_id,
                "space_type": drive.get("space_type"),
                "cloud_folder_id": parent_id,
                "cloud_folder_path": parent_path,
                "raw_files": files,
            })
    return leaf_folders


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


def _download_one_file(cli: PdsCli, cloud_path: str, raw_item: dict[str, Any], download_root: Path) -> dict[str, Any]:
    """同步阻塞调用(内部是subprocess)，调用方要丢进线程池。

    不用pds_sync.core.download_item：它会把本地路径套一层"space_type-drive_id"
    目录，但PDS的list-file返回里根本没有space_type这个字段(只有list-drive
    才有)，导致这层目录永远是没有实际意义的"unknown-<drive_id>"(见
    2026-09-25的讨论)。这里直接用BFS遍历时已经知道的cloud_path，本地路径
    就是download_root+cloud_path，干净且不用再多调一次resolve-path API。
    哈希跳过(_file_matches)/冲突改名(_conflict_path)逻辑跟download_item一致，
    直接复用pds_sync.core里现成的纯函数。"""
    drive_id = str(raw_item.get("drive_id") or "")
    file_id = str(raw_item.get("file_id") or "")
    root = Path(download_root).resolve()
    relative = PurePosixPath(cloud_path).relative_to("/")
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"云端路径逃出了下载目录: {cloud_path}")
    target.parent.mkdir(parents=True, exist_ok=True)

    status = "downloaded"
    if target.exists():
        if target.is_file() and _file_matches(raw_item, target):
            return {"file_id": file_id, "cloud_path": cloud_path, "local_path": str(target), "status": "skipped"}
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
            "file_id": file_id, "cloud_path": cloud_path, "local_path": str(target),
            "size": local_size, "status": status,
        }
    finally:
        if temporary.exists():
            temporary.unlink()


async def _download_folder_files(folder: dict[str, Any], cli: PdsCli) -> list[dict[str, Any]]:
    """下载文件夹里全部当前文件；已经下载过的会按哈希跳过(_download_one_file
    内部逻辑)，所以对"完成后又有新增"这种情况重新调一次也是安全的、只会
    真正下载新文件。

    注意：不直接用传进来的cli(那个是给list-file这类快调用用的，超时只有
    60s)——航测视频单个文件常常几GB，60s传不完就会被判超时(见2026-09-25
    实际报警：'Aliyun CLI timed out after 60s')，这里单独用
    _build_download_cli()构造一个大超时的实例。"""
    folder_path = folder["cloud_folder_path"]
    download_cli = _build_download_cli()
    downloaded: list[dict[str, Any]] = []
    for raw_item in folder["raw_files"]:
        cloud_path = f"{folder_path.rstrip('/')}/{raw_item.get('name')}"
        try:
            result = await asyncio.to_thread(
                _download_one_file, download_cli, cloud_path, raw_item, Path(settings.aliyun_download_dir)
            )
        except (PdsError, OSError, ValueError) as exc:
            await send_feishu_message(f"⚠️ 阿里云下载失败：{folder_path} 里的 {raw_item.get('name')}：{exc}")
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
            "cloud_path": result.get("cloud_path"),
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


async def _trigger_download(
    session: AsyncSession,
    cli: PdsCli,
    folder: dict[str, Any],
    file_entries: list[dict[str, Any]],
    scan_completed_at: datetime,
) -> None:
    folder_path = folder["cloud_folder_path"]

    if not _validate_top_level_folder(folder_path):
        # 次顶层目录名不符合"年份_城市_AerialVideos_季度"规范，直接拒绝下载。
        # 只在第一次检测到时报警+记一条issue事件，避免同一个问题每6小时刷一次飞书；
        # 人工把目录名改对之后，下次心跳会重新判定，不需要额外的"解除标记"操作
        already_flagged = await _latest_issue_event(
            session, folder["cloud_folder_id"], "invalid_top_level_name"
        )
        if already_flagged is None:
            top_name = _top_level_folder_name(folder_path)
            session.add(AliyunSyncEvent(
                event_type="issue",
                issue_type="invalid_top_level_name",
                cloud_folder_id=folder["cloud_folder_id"],
                cloud_folder_path=folder_path,
                videos=[],
                notified_at=datetime.now(timezone.utc),
            ))
            print(
                f"[aliyun] 次顶层文件夹命名不符合规范，拒绝下载：{folder_path} "
                f"(次顶层目录名={top_name})",
                flush=True,
            )
            await send_feishu_message(
                f"⚠️ 文件夹命名不符合规范(应为 年份_城市_AerialVideos_季度，"
                f"如 2026_Changchun_AerialVideos_Q1)，已跳过下载：{folder_path}"
            )
        return

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
        local_folder = Path(settings.aliyun_download_dir).resolve() / PurePosixPath(folder_path).relative_to("/")
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
    """完整跑一次：遍历全部drive的全部叶子文件夹，逐个记心跳、判断是否要下载/报警。

    每个文件夹处理完就commit一次，不是等全部文件夹跑完才一次性commit——
    单个文件夹的下载可能要跑很久(大文件)，中途被Ctrl-C/进程被杀的话，
    只在最后commit会导致这一整轮扫描、包括之前已经处理完的其他文件夹的
    心跳记录，全部跟着白跑(2026-09-25发现aliyun_sync_events是空的就是
    因为这个：中途Ctrl-C，从来没走到过最后那次commit)。"""
    cli = _build_cli()
    drives = await asyncio.to_thread(list_drives, cli, settings.aliyun_spaces)
    folder_count = 0
    for drive in drives:
        leaf_folders = await asyncio.to_thread(_walk_leaf_folders, cli, drive)
        for folder in leaf_folders:
            await _process_folder(session, cli, folder)
            await session.commit()
            folder_count += 1
    print(f"[aliyun] 扫描完成: drive_count={len(drives)} folder_count={folder_count}", flush=True)
