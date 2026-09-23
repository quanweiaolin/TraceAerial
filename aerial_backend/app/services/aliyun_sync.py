"""
阿里云PDS自动下载流程。计划由APScheduler每6小时调一次 run_aliyun_scan。

流程：
  1. 遍历PDS每个可访问的drive，找出全部"叶子文件夹"(不含子文件夹、直接装着
     MP4/SRT等文件的目录) —— 这就是要监控的最小单位
  2. 每个叶子文件夹跟aliyun_folder_scans里它自己最新一行比大小/文件数，
     写一条新的心跳记录(变了才存完整文件列表，见add_aliyun_folder_scans.sql)
  3. 还没判定完成(aliyun_sync_events里没有它的folder_ready事件)的文件夹：
     检查是否连续N次心跳都没变化(N=settings.aliyun_stability_scans_required)，
     稳定了就检查磁盘空间→下载到本地暂存目录→写folder_ready事件
  4. 已经判定完成的文件夹继续被监控：
     - 又有新文件/变大 → 飞书提示(不是错误，继续下载新增部分) +
       写issue事件(reupload_after_complete)
     - 变小/文件不见了 → 对比上一次有完整文件列表的心跳，找出具体丢了哪个
       文件 → 飞书提示 + 写issue事件(file_deleted)

不做的事(明确暂缓，见项目里的讨论记录)：
  - unknown_uploader检测(飞手归属要靠按人分文件夹的真实结构，测试数据还没有)
  - 下载完之后自动整理/移到NAS(SSD到HDD的搬运，还没规划)
  - 下载后自动写入videos表(前置检查条件还没做，先只落地文件)
"""
from __future__ import annotations

import asyncio
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

REPO_ROOT = Path(__file__).resolve().parents[3]
PDS_TOOL_ROOT = REPO_ROOT / "extern" / "pds-drive-sync"
if str(PDS_TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(PDS_TOOL_ROOT))

from pds_sync.core import (  # noqa: E402  (必须在sys.path插入之后才能import)
    ITEM_PROJECTION,
    PdsCli,
    PdsError,
    _paginate,
    download_item,
    list_drives,
)


def _cli_binary_path() -> Path:
    configured = settings.aliyun_cli_binary
    if configured:
        return Path(configured)
    return PDS_TOOL_ROOT / ".tools" / "aliyun"


def _build_cli() -> PdsCli:
    return PdsCli(_cli_binary_path())


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
    downloaded: list[dict[str, Any]] = []
    for raw_item in folder["raw_files"]:
        try:
            result = await asyncio.to_thread(download_item, cli, raw_item, Path(settings.aliyun_download_dir))
        except (PdsError, OSError, ValueError) as exc:
            await send_feishu_message(f"⚠️ 阿里云下载失败：{folder_path} 里的 {raw_item.get('name')}：{exc}")
            continue
        downloaded.append({
            "cloud_file_id": result.get("file_id"),
            "cloud_path": result.get("cloud_path"),
            "content_hash": raw_item.get("content_hash"),
            "content_hash_algo": raw_item.get("content_hash_name"),
            "local_path": result.get("local_path"),
        })
    download_finished_at = datetime.now(timezone.utc)

    session.add(AliyunSyncEvent(
        event_type="folder_ready",
        cloud_folder_id=folder["cloud_folder_id"],
        cloud_folder_path=folder_path,
        videos=downloaded,
        scan_completed_at=scan_completed_at,
        download_started_at=download_started_at,
        download_finished_at=download_finished_at,
        notified_at=datetime.now(timezone.utc),
    ))
    await send_feishu_message(
        f"✅ 阿里云文件夹上传完成，已下载 {len(downloaded)} 个文件：{folder_path}"
    )


async def _handle_reupload(session: AsyncSession, folder: dict[str, Any], file_entries: list[dict[str, Any]]) -> None:
    folder_path = folder["cloud_folder_path"]
    session.add(AliyunSyncEvent(
        event_type="issue",
        issue_type="reupload_after_complete",
        cloud_folder_id=folder["cloud_folder_id"],
        cloud_folder_path=folder_path,
        videos=[],
        notified_at=datetime.now(timezone.utc),
    ))
    await send_feishu_message(
        f"⚠️ 已判定完成的文件夹又有新上传，继续下载：{folder_path}"
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
        await _handle_reupload(session, folder, file_entries)
    else:
        await _handle_possible_deletion(session, folder, file_entries)


async def run_aliyun_scan(session: AsyncSession) -> None:
    """完整跑一次：遍历全部drive的全部叶子文件夹，逐个记心跳、判断是否要下载/报警。"""
    cli = _build_cli()
    drives = await asyncio.to_thread(list_drives, cli, settings.aliyun_spaces)
    for drive in drives:
        leaf_folders = await asyncio.to_thread(_walk_leaf_folders, cli, drive)
        for folder in leaf_folders:
            await _process_folder(session, cli, folder)
    await session.commit()
