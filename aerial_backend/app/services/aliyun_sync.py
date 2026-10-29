"""
阿里云PDS自动下载流程。计划由APScheduler每6小时调一次 run_aliyun_scan。

云端目录结构：飞手上传时只负责建"飞行会话文件夹"(命名规范见
SESSION_FOLDER_RE，例如 changchun_panshiroad_m3e_200m_wangziyu_20260912)，
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
     - 变小/文件不见了(云端这边) → 对比上一次有完整文件列表的心跳，找出
       具体丢了哪个文件 → 飞书提示 + 写issue事件(file_deleted)
     - 云端没变(这是大多数心跳的情况) → 额外检查一下本地磁盘上这些文件
       是否还真的在(2026-10-29真实事故：本地整理文件夹时把36个MP4弄丢了，
       云端这边完全没变，之前的逻辑在这里直接return，完全没有机会发现本地
       少了东西)。本地缺了就重新触发一次下载(复用_download_with_
       reconciliation，已经存在且内容对的文件会被底层_file_matches按哈希
       跳过，只会真正传输缺的那部分) + 写issue事件(local_files_missing)

不做的事(明确暂缓，见项目里的讨论记录)：
  - unknown_uploader检测(飞手归属要靠按人分文件夹的真实结构，测试数据还没有)
"""
from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AliyunFolderScan, AliyunSyncEvent
from app.services.notifier import send_feishu_message
from app.services.scanner import run_scan
from app.services.session_naming import (
    SESSION_FOLDER_DEVICES,
    SESSION_FOLDER_RE,
    derive_category_folder,
    diagnose_session_folder_name,
    looks_like_session_candidate,
)

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

PREFIX_ALIYUN = "【阿里云下载】"  # 2026-10-28起，这个模块发的飞书消息统一加这个前缀
ALIYUN_SCAN_INTERVAL_HOURS = 6  # scheduled_aliyun_scan的心跳间隔，跟main.py注册
# 这个定时任务时传的interval hours保持同一个值(main.py直接import这个常量用，
# 不要在那边另外写死一个6)。"判定完成后静默多久才算真稳定"这个计算
# (settings.aliyun_stability_scans_required * 这个值)依赖这俩数字是同一个
# 真实在跑的心跳节奏，不是两份互相不知道对方的硬编码(2026-10-28教训：
# 一开始给"哪些文件夹可以开始"那个查询写死了"10 days"字面量，改心跳配置
# 容易忘记同步，后来改成直接算这两个值，不再有写死的数字)


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
        f"{PREFIX_ALIYUN} ⚠️ 磁盘空间不足，暂停阿里云下载：{path} 所在盘剩余 {free_gb:.1f} GiB，"
        f"低于阈值 {threshold:.0f} GiB"
    )
    return False


def _is_within_scan_roots(path: Path) -> bool:
    """下载落地的目录是不是在AERIAL_SCAN_ROOTS配置的某一个根目录之下(或者
    就是其中一个根目录本身)——只有在这个范围内才允许自动扫描进videos库，
    见_trigger_download里的调用。"""
    resolved = path.resolve()
    for root in settings.scan_roots:
        root_path = Path(root).resolve()
        if resolved == root_path or resolved.is_relative_to(root_path):
            return True
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
                    "category_folder": derive_category_folder(match),
                    "session_name": name,
                    "raw_files": raw_files,
                })
            elif looks_like_session_candidate(name):
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


def _cleanup_stale_download_temp_files(directory: Path) -> None:
    """清理底层aliyun CLI自己内部下载机制留下的.pds-download-*残留文件——
    这个命名不是我们自己代码起的(搜过pds_sync源码里没有这个约定)，是aliyun
    CLI那个二进制自己内部download-to-local时用的临时文件机制；下载子进程被
    kill/超时中断时会在目标目录留下这类孤儿文件，我们自己的_download_one_file
    完全不知道它的存在、也不会去清理(只会清理自己起名的.{name}.{hex}.part)。
    只清理修改时间早于aliyun_download_timeout_seconds的——Python
    subprocess.run(timeout=...)超时会真的kill掉子进程(标准库保证)，所以
    超过这个时长还在的，一定不会再有别的下载还在写它，可以放心删，不会跟
    正在进行中的下载撞车(2026-10-22发现：5个这样的孤儿文件，最早的是10-7
    留下的，合计浪费小几十G磁盘空间，一直没人清)。"""
    cutoff = time.time() - settings.aliyun_download_timeout_seconds
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.name.startswith(".pds-download-"):
            continue
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                print(f"[aliyun] 清理掉过期的下载残留文件: {entry}", flush=True)
        except OSError:
            continue


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
    _cleanup_stale_download_temp_files(target.parent)

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
            await send_feishu_message(f"{PREFIX_ALIYUN} ⚠️ 下载失败：{folder_path} 里的 {relative_path}：{exc}")
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


def _missing_local_files(folder: dict[str, Any], download_root: Path) -> list[dict[str, Any]]:
    """已经判定完成下载的文件夹，按云端这次心跳报的每个文件，检查预期的本地
    路径(跟_download_folder_files算本地路径用的是同一套拼接逻辑)是否真的
    还是一个文件——这是唯一可靠的"到底还在不在"的判据，不靠videos表的
    sha1列：sha1只有在文件真实存在、被读过的时候才能算出来，文件本来就
    丢了的话sha1本来也会是NULL，拿"sha1缺失"去判断"文件丢没丢"是本末倒置
    (2026-10-29真实事故发现：丢失的36个视频sha1全是NULL，正是因为文件
    已经不存在，backfill脚本读不到文件没法算，不是别的原因)。

    是同步阻塞调用(逐个is_file()，走NAS网络IO)，调用方要丢进线程池。
    返回缺失的原始文件条目列表(跟folder['raw_files']同结构)，方便直接
    复用现有下载pipeline重新下载——已经存在且内容对得上的文件，底层
    _file_matches会按哈希自动跳过，不会重复下载。"""
    missing = []
    for raw_item in folder["raw_files"]:
        relative_path = raw_item.get("relative_path") or raw_item.get("name")
        local_path = download_root / folder["category_folder"] / folder["session_name"] / relative_path
        if not local_path.is_file():
            missing.append(raw_item)
    return missing


RECONCILE_MAX_ATTEMPTS = 3  # 文件数核对不上时最多重试几次，见_download_with_reconciliation


def _count_local_files(folder: dict[str, Any], download_root: Path) -> int:
    """数一下这个会话文件夹现在磁盘上实际有多少个文件(递归，排除以"."开头的——
    我们自己的.{name}.{hex}.part临时文件、底层CLI的.pds-download-*残留、
    .DS_Store这类系统文件都不算)，用来跟云端这次心跳报的文件数核对。"""
    target_dir = download_root / folder["category_folder"] / folder["session_name"]
    if not target_dir.is_dir():
        return 0
    return sum(1 for p in target_dir.rglob("*") if p.is_file() and not p.name.startswith("."))


async def _download_with_reconciliation(
    folder: dict[str, Any], cli: PdsCli
) -> tuple[list[dict[str, Any]], bool]:
    """下载这个文件夹的全部当前文件，下载完之后额外核对一次"云端这次心跳报的
    文件数 vs 本地实际落盘的文件数"——每个文件下载异常_download_one_file自己
    能感知(见_download_folder_files里的try/except)，但"这次调用里没有一个
    文件报异常"不等于"数出来的文件总数真的对得上"，这个核对是多一道保险。
    对不上就重试(最多RECONCILE_MAX_ATTEMPTS次，每次都是完整重跑
    _download_folder_files——已经下载对的文件靠哈希跳过，不会重复下载，
    只会真正重试还缺的那些，代价很小)，重试次数用完还是对不上，返回
    reconciled=False，调用方负责报警，不在这里发消息(2026-10-22达成一致：
    对不上先重试，3次不行再报警)。"""
    expected_count = len(folder["raw_files"])
    downloaded: list[dict[str, Any]] = []
    for attempt in range(1, RECONCILE_MAX_ATTEMPTS + 1):
        downloaded = await _download_folder_files(folder, cli)
        actual_count = _count_local_files(folder, Path(settings.aliyun_download_dir))
        if actual_count == expected_count:
            return downloaded, True
        print(
            f"[aliyun] 文件数核对不上(第{attempt}/{RECONCILE_MAX_ATTEMPTS}次)："
            f"云端{expected_count}个，本地实际{actual_count}个，{folder['cloud_folder_path']}",
            flush=True,
        )
    return downloaded, False


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
    报警+记一条issue事件；人工把目录名改对之后，下次心跳会重新判定，不需要
    额外的"解除标记"操作。这类文件夹不会被收集文件/不会进入心跳监控，是
    终态的拒绝。

    去重判断依据是"这次的文件夹名"是不是跟上次报警时记录的名字完全一样——
    不是"报没报过"。原来的版本只要报过一次就永久不再报，结果学生改名字
    (哪怕改完还是不对，比如大小写没改对)之后系统完全没反应，第二次错误
    被永久噤声了(2026-10-06发现此问题)。现在只有"名字没变、还是上次那个
    错误"才跳过，名字只要变了(哪怕换了种错法)就要重新报一次。"""
    cloud_folder_id = candidate["cloud_folder_id"]
    folder_path = candidate["cloud_folder_path"]
    name = candidate["name"]
    last_event = await _latest_issue_event(session, cloud_folder_id, "invalid_session_name")
    if last_event is not None and last_event.videos == [{"name": name}]:
        return
    # 逐字段诊断，精确到"哪个字段、写的是什么、应该长什么样"，不是笼统的
    # "不符合规范"——人一看报警就知道要去云端哪个文件夹改哪个字段
    field_reasons = diagnose_session_folder_name(name)
    reason = "；".join(field_reasons)
    session.add(AliyunSyncEvent(
        event_type="issue",
        issue_type="invalid_session_name",
        cloud_folder_id=cloud_folder_id,
        cloud_folder_path=folder_path,
        videos=[{"name": name}],
        notified_at=datetime.now(timezone.utc),
    ))
    print(f"[aliyun] 命名不符合规范，拒绝下载：{folder_path}\n  {reason}", flush=True)
    await send_feishu_message(f"{PREFIX_ALIYUN} ⚠️ 文件夹命名不符合规范，已跳过下载：{folder_path}\n{reason}")


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
    downloaded, reconciled = await _download_with_reconciliation(folder, cli)
    download_finished_at = datetime.now(timezone.utc)
    all_succeeded = all(d.get("download_status") != "failed" for d in downloaded) and reconciled
    _log_download_detail("文件夹判定上传完成，开始下载", folder_path, downloaded)

    scanned_in = True  # 默认True：没必要扫/按策略跳过扫描，都不算"失败"
    if all_succeeded:
        # 全部文件下载成功后，才把这个文件夹扫描进videos库——跟scanner.py的
        # 常规定时扫盘复用同一套逻辑(哈希去重/瘦身检测/飞书通知都一样)，
        # 只是触发时机和范围精确到"刚下载完的这一个文件夹"，不用等下一次
        # 全量扫盘。scan失败也算这次没完成，不标记download_finished_at，
        # 下次心跳(云端内容没变、还在"稳定"状态)会自动整体重试
        local_folder = Path(settings.aliyun_download_dir).resolve() / folder["category_folder"] / folder["session_name"]
        if not _is_within_scan_roots(local_folder):
            # 防火墙：aliyun_download_dir必须明确配在AERIAL_SCAN_ROOTS范围内，
            # 才会自动扫描进videos库。2026-10-09发现：测试时download_dir临时
            # 指向过SSD暂存目录(不在scan_roots内)，照样被扫进了正式的videos
            # 表，污染了生产数据。以后download_dir指向任何不在scan_roots里的
            # 地方(比如临时测试路径)，下载依然正常完成，但不会自动入库，
            # 避免同样的事故重演
            print(
                f"[aliyun] 下载目录不在AERIAL_SCAN_ROOTS范围内，跳过扫描入库(不算失败)：{local_folder}",
                flush=True,
            )
        else:
            try:
                await run_scan(session, str(local_folder))
            except Exception as exc:  # noqa: BLE001  范围特意放宽：扫描入库这一步绝不能让整个扫描循环中断
                scanned_in = False
                print(f"[aliyun] 下载完成但扫描入库失败：{local_folder}  {exc}", flush=True)
                await send_feishu_message(f"{PREFIX_ALIYUN} ⚠️ 下载完成但扫描入库失败：{folder_path}\n{exc}")
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
            f"{PREFIX_ALIYUN} ✅ 文件夹下载完成并已扫描入库：{folder_path}\n{_download_summary_line(downloaded)}"
        )
    elif not reconciled:
        # 跟"处理未全部成功"分开报——那条是单个文件下载/扫描报了异常，这条是
        # 没有任何异常、但重试RECONCILE_MAX_ATTEMPTS次之后文件数还是对不上，
        # 更可能是真实数据缺失，需要人工确认，不是"下次心跳自动重试"就能
        # 解决的(2026-10-22达成一致)
        await send_feishu_message(
            f"{PREFIX_ALIYUN} 🚨 文件夹下载后文件数核对不上(已重试{RECONCILE_MAX_ATTEMPTS}次)，"
            f"请人工确认：{folder_path}\n{_download_summary_line(downloaded)}"
        )
    else:
        await send_feishu_message(
            f"{PREFIX_ALIYUN} ⚠️ 文件夹处理未全部成功，下次心跳会自动重试：{folder_path}\n{_download_summary_line(downloaded)}"
        )


async def _handle_reupload(
    session: AsyncSession,
    cli: PdsCli,
    folder: dict[str, Any],
    file_entries: list[dict[str, Any]],
    ready_event: AliyunSyncEvent,
) -> None:
    folder_path = folder["cloud_folder_path"]
    if not await ensure_enough_space(settings.aliyun_download_dir):
        return  # 磁盘不够，飞书已经发过了，等下次心跳再试

    download_started_at = datetime.now(timezone.utc)
    downloaded, reconciled = await _download_with_reconciliation(folder, cli)
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
        f"{PREFIX_ALIYUN} ⚠️ 已判定完成的文件夹又有新上传：{folder_path}\n{_download_summary_line(downloaded)}"
    )

    # 2026-10-28新加：这次重新上传离"当初判定完成"已经超过稳定期阈值
    # (settings.aliyun_stability_scans_required * 心跳间隔，眼下是10天，
    # 跟/aliyun/folders-ready用的是同一个算法，以后改配置两边自动一起变)，
    # 说明飞手不是"刚传完、隔几个小时又补了点"这种正常节奏，而是系统/团队
    # 都已经把这个文件夹当成"早就完事了"很久之后又冒出来，更可能是飞手
    # 本身操作习惯有问题，单独点名报出来，方便拿去跟具体的人宣贯
    stable_hours = settings.aliyun_stability_scans_required * ALIYUN_SCAN_INTERVAL_HOURS
    is_overdue = (
        ready_event.download_finished_at is not None
        and download_started_at - ready_event.download_finished_at > timedelta(hours=stable_hours)
    )
    if is_overdue:
        session_name = folder["session_name"]
        match = SESSION_FOLDER_RE.match(session_name)
        uploader = match.group("person") if match else "未知"
        await send_feishu_message(
            f"{PREFIX_ALIYUN} 📢 已判定完成上传的文件夹再次上传，"
            f"上传者为 {uploader}，上传文件夹为 {folder_path}，应对此进行宣贯"
        )

    if not reconciled:
        # 同_trigger_download那边的道理：重试RECONCILE_MAX_ATTEMPTS次后文件数
        # 还是对不上，单独报一条，别混在上面那条"又有新上传"里不显眼
        # (2026-10-22达成一致)
        await send_feishu_message(
            f"{PREFIX_ALIYUN} 🚨 文件夹下载后文件数核对不上(已重试{RECONCILE_MAX_ATTEMPTS}次)，"
            f"请人工确认：{folder_path}\n{_download_summary_line(downloaded)}"
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
        f"{PREFIX_ALIYUN} ⚠️ 已判定完成的文件夹里有文件不见了：{folder_path}，疑似被删除：{detail}"
    )


async def _handle_local_files_missing(
    session: AsyncSession, cli: PdsCli, folder: dict[str, Any], missing: list[dict[str, Any]]
) -> None:
    """已经判定完成下载的文件夹，云端这次心跳跟上次比完全没变，但本地磁盘
    上该有的文件却不见了(2026-10-29真实事故：有人整理文件夹时把一个session
    底下36个MP4弄丢了，只剩SRT，云端那份完好无损)。常规心跳只比对"云端"
    这次跟上次的大小/文件数，云端没变就直接return，从来没有机会检查本地
    磁盘的实际情况——这个函数就是补这个缺口的。

    直接对整个文件夹重跑一次_download_with_reconciliation，不是只下载
    missing这几个：已经存在且内容对得上的文件会被底层_file_matches按哈希
    自动跳过(比较本地文件内容跟云端content_hash，跟我们videos表的sha1列
    完全无关)，所以重跑一次只会真正传输缺失的部分，代价可控，逻辑也更
    简单(不用维护"只下载这个子集"的另一套路径拼接代码)。

    重新扫描进videos库这一步：这些视频的sha256早就在库里(当年第一次下载
    时就入库了，只是物理文件后来不见了)，scanner.py按sha256识别出"已存在"，
    不会产生重复记录，只是物理文件补回来了。sha1列还会是NULL——backfill_
    video_sha1.py下次跑的时候文件已经在了，自然会补上，这里不用管。"""
    folder_path = folder["cloud_folder_path"]
    missing_names = [m.get("relative_path") or m.get("name") for m in missing]
    print(
        f"[aliyun] 已完成的文件夹本地文件丢失(云端未变)，重新下载：{folder_path}，"
        f"缺失{len(missing)}个: {missing_names}",
        flush=True,
    )
    await send_feishu_message(
        f"{PREFIX_ALIYUN} 🚨 已判定完成的文件夹本地磁盘文件丢失(云端完好)，正在重新下载："
        f"{folder_path}\n缺失{len(missing)}个文件: " + "、".join(str(n) for n in missing_names[:10])
        + ("等" if len(missing_names) > 10 else "")
    )

    if not await ensure_enough_space(settings.aliyun_download_dir):
        return  # 磁盘不够，飞书已经发过了，等下次心跳再试

    download_started_at = datetime.now(timezone.utc)
    downloaded, reconciled = await _download_with_reconciliation(folder, cli)
    download_finished_at = datetime.now(timezone.utc)
    _log_download_detail("本地文件丢失，重新下载", folder_path, downloaded)

    scanned_in = True
    if reconciled:
        local_folder = Path(settings.aliyun_download_dir).resolve() / folder["category_folder"] / folder["session_name"]
        if _is_within_scan_roots(local_folder):
            try:
                await run_scan(session, str(local_folder))
            except Exception as exc:  # noqa: BLE001  扫描入库这一步绝不能让整个扫描循环中断
                scanned_in = False
                print(f"[aliyun] 本地文件补回后扫描入库失败：{local_folder}  {exc}", flush=True)
                await send_feishu_message(f"{PREFIX_ALIYUN} ⚠️ 本地文件补回后扫描入库失败：{folder_path}\n{exc}")

    session.add(AliyunSyncEvent(
        event_type="issue",
        issue_type="local_files_missing",
        cloud_folder_id=folder["cloud_folder_id"],
        cloud_folder_path=folder_path,
        videos=downloaded,
        download_started_at=download_started_at,
        download_finished_at=download_finished_at if reconciled and scanned_in else None,
        notified_at=datetime.now(timezone.utc),
    ))

    if reconciled and scanned_in:
        await send_feishu_message(
            f"{PREFIX_ALIYUN} ✅ 本地丢失文件已重新下载并扫描入库：{folder_path}\n{_download_summary_line(downloaded)}"
        )
    elif not reconciled:
        await send_feishu_message(
            f"{PREFIX_ALIYUN} 🚨 本地丢失文件重新下载后文件数仍核对不上(已重试{RECONCILE_MAX_ATTEMPTS}次)，"
            f"请人工确认：{folder_path}\n{_download_summary_line(downloaded)}"
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
        # 云端没变(大多数心跳都是这种情况)，但不代表本地磁盘上的文件就一定
        # 还在——2026-10-29真实事故：本地整理文件夹把36个MP4弄丢了，云端
        # 完全没变，这个分支以前直接return，完全没机会发现。按预期路径逐个
        # 查一下(走NAS网络IO，丢进线程池)，缺了就重新触发下载
        missing = await asyncio.to_thread(
            _missing_local_files, folder, Path(settings.aliyun_download_dir)
        )
        if missing:
            await _handle_local_files_missing(session, cli, folder, missing)
        return
    if file_count > previous.file_count or size_bytes > previous.size_bytes:
        await _handle_reupload(session, cli, folder, file_entries, ready_event)
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
