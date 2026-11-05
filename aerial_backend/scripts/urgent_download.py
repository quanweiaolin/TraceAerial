"""给学生用的"紧急下载"工具：输入一个完整会话文件夹名
(city_site_drone_height_person_date)，立刻从阿里云PDS下载+扫描入库，
不用等常规自动化流程(aliyun_sync.py)那个"连续N次心跳没变化才算上传
完成"的稳定期判断。

跟常规流程最大的区别、也是使用者需要知道的风险：跳过稳定期意味着可能
抓到一份云端还没传完的数据。这个风险这个脚本消除不了——常规流程后续的
reupload_after_complete检测会在云端真的又有新文件时发现并提醒，但没法
保证"现在抓到的这一份"是完整的(2026-11-03跟用户确认，这是"紧急"模式
主动接受的代价，不是bug)。

两阶段设计：
  1. 校验阶段(同步，占用终端几秒钟)：会话名格式完全匹配 + 真的能在某个
     云端drive的根目录下找到这个文件夹。任何一步不通过，终端直接报错
     退出，不进入下一步、不发任何飞书消息、不留任何记录。
  2. 下载阶段(校验通过后，脚本把自己fork成一个独立的后台进程去跑，前台
     立刻把终端还给用户，不会占用——不需要用户自己nohup/disown)：
     下载 -> 扫描入库 -> sha1校验 -> 登记folder_ready事件(这样以后这个
     文件夹如果又被常规自动化流程的稳定期扫到，会识别出"已经有人处理
     过了"直接跳过，不会冲突/重复下载) -> 发"完成"飞书消息。任何一步
     失败都发一条说明原因的飞书消息，并且不登记folder_ready(避免"没做
     完却被当成done"，污染状态)。

重启/中断容错(2026-11-03跟用户确认的方案，不做持久化任务队列，保持
简单)：后台进程挂在FastAPI主进程之外，不受主进程重启影响；但如果这个
脚本自己的后台进程被杀/机器重启，没有自动恢复机制——用户看长时间没收到
"完成"飞书消息，直接重新跑一遍命令即可。这样做是安全的：下载层按哈希
跳过已存在且内容对的文件，入库层有SAVEPOINT防止哈希冲突崩溃，重试不会
产生重复下载或脏数据。

安全边界：使用者(学生)实际连库用的是ad4che这个有完整写权限的账号(不是
reader)，数据库权限没法兜底，所有安全保证只能靠这个脚本自己的输入校验：
会话名必须SESSION_FOLDER_RE完全匹配(正则字符集只允许小写字母/数字，
没法塞入/或..做路径穿越)；只在drive根目录下解析、不做递归搜索(找不到
就是找不到，不去四处搜index带来额外的API消耗/攻击面)；下载落盘路径
复用_download_one_file现成的target.is_relative_to(root)兜底检查。

用法：
    uv run python aerial_backend/scripts/urgent_download.py \
        --session-name shanghai_jianchuanroad_mavic4pro_120m_wangxiangyu_20261104
"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # aerial_backend/

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionContext  # noqa: E402
from app.models import AliyunSyncEvent, Video  # noqa: E402
from app.services.aliyun_sync import (  # noqa: E402
    PREFIX_ALIYUN,
    RECONCILE_MAX_ATTEMPTS,
    _build_cli,
    _count_local_files,
    _download_folder_files,
    _gather_all_files,
    _is_within_scan_roots,
    ensure_enough_space,
)
from app.services.notifier import send_feishu_message  # noqa: E402
from app.services.scanner import VIDEO_EXTENSIONS, run_scan  # noqa: E402
from app.services.session_naming import (  # noqa: E402
    SESSION_FOLDER_DEVICES,
    SESSION_FOLDER_RE,
    derive_category_folder,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PDS_TOOL_ROOT = REPO_ROOT / "extern" / "pds-drive-sync"
if str(PDS_TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(PDS_TOOL_ROOT))

from pds_sync.core import PdsError, list_drives  # noqa: E402

RESOLVE_PROJECTION = (
    "file.{type:type,size:size,content_hash:content_hash,"
    "content_hash_name:content_hash_name,file_id:file_id,name:name}"
)


def _resolve_at_root(session_name: str) -> tuple[str, str]:
    """只在每个drive的根目录下找这个会话名(不做递归搜索，见模块docstring的
    安全设计)。返回(drive_id, cloud_folder_id)；找不到/有歧义直接
    raise SystemExit，终端报错退出。"""
    cli = _build_cli()
    drives = list_drives(cli, settings.aliyun_spaces)
    matches: list[tuple[str, str]] = []
    for drive in drives:
        drive_id = str(drive.get("drive_id") or "")
        try:
            info = cli.pds([
                "resolve-path", "--drive-id", drive_id, "--path", f"/{session_name}",
                "--cli-query", RESOLVE_PROJECTION,
            ])
        except PdsError:
            continue
        if isinstance(info, dict) and info.get("file_id") and info.get("type") == "folder":
            matches.append((drive_id, str(info["file_id"])))
    if not matches:
        raise SystemExit(
            f"在云端任何drive的根目录下都找不到文件夹 /{session_name}\n"
            "（只查根目录，不做递归搜索——如果确定它在云端但嵌套在别的目录里，"
            "联系管理员手动处理，或者把它挪到drive根目录下再重试）"
        )
    if len(matches) > 1:
        raise SystemExit(f"这个会话名在多个drive里都存在，有歧义，联系管理员处理：{matches}")
    return matches[0]


def _to_scan_path(download_local_path: str) -> str:
    """把_download_one_file返回的本地路径(永远是以aliyun_download_dir为根)
    换算成以aliyun_scan_dir为根的等价路径——两者是同一份物理文件，只是
    run_scan记进videos.storage_path时用的是scan_dir(bindfs的直连等价
    路径，见app/config.py里aliyun_scan_dir的注释)，查DB必须按这个换算后
    的路径查，不能直接拿下载时的路径去查(2026-11-05测试这个脚本时发现：
    没换算直接查一条都查不到，会被误判成sha1全部对不上)。"""
    download_root = Path(settings.aliyun_download_dir).resolve()
    scan_root = Path(settings.aliyun_scan_dir or settings.aliyun_download_dir).resolve()
    relative = Path(download_local_path).resolve().relative_to(download_root)
    return str(scan_root / relative)


async def _split_by_db_cache(
    session: AsyncSession, folder: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按DB里已经有的sha1/sha256，把这次云端报的文件列表拆成两份：确认
    能跳过的(不用碰_download_one_file，省掉它内部_file_matches()重新读
    整个文件验证哈希的开销——对几个GB的视频来说这是真实的时间差)，和
    真的需要走下载/验证流程的(DB里没有、或者有但哈希对不上，一律走
    常规那套保守验证，不弱化)。

    信任前提：本地文件自扫描入库以来没被改坏过(这套系统基本只增不改，
    风险很低但不是零，2026-11-05跟用户讨论过这个取舍)。只改这个脚本
    自己的逻辑，不碰_download_one_file/_file_matches那条常规自动化
    流程共用的路径——出问题影响范围只限于"紧急下载"这一个功能。"""
    scan_root = Path(settings.aliyun_scan_dir or settings.aliyun_download_dir).resolve()
    by_scan_path: dict[str, dict[str, Any]] = {}
    for raw_item in folder["raw_files"]:
        relative_path = raw_item.get("relative_path") or raw_item.get("name")
        local_relative = f"{folder['category_folder']}/{folder['session_name']}/{relative_path}"
        by_scan_path[str(scan_root / local_relative)] = raw_item

    known: dict[str, tuple[bytes | None, bytes | None]] = {}
    if by_scan_path:
        result = await session.execute(
            select(Video.storage_path, Video.sha1, Video.sha256).where(Video.storage_path.in_(by_scan_path.keys()))
        )
        known = {row.storage_path: (row.sha1, row.sha256) for row in result}

    confirmed_skip: list[dict[str, Any]] = []
    need_download: list[dict[str, Any]] = []
    download_root = Path(settings.aliyun_download_dir).resolve()
    for scan_path, raw_item in by_scan_path.items():
        algo = str(raw_item.get("content_hash_name") or "").lower()
        expected_hex = str(raw_item.get("content_hash") or "").lower()
        sha1_val, sha256_val = known.get(scan_path, (None, None))
        matched = (
            (algo == "sha1" and bool(sha1_val) and sha1_val.hex() == expected_hex)
            or (algo == "sha256" and bool(sha256_val) and sha256_val.hex() == expected_hex)
        )
        if not matched:
            need_download.append(raw_item)
            continue
        relative_path = raw_item.get("relative_path") or raw_item.get("name")
        local_relative = f"{folder['category_folder']}/{folder['session_name']}/{relative_path}"
        confirmed_skip.append({
            "cloud_file_id": raw_item.get("file_id"),
            "cloud_path": f"{folder['cloud_folder_path'].rstrip('/')}/{relative_path}",
            "content_hash": raw_item.get("content_hash"),
            "content_hash_algo": raw_item.get("content_hash_name"),
            "local_path": str(download_root / local_relative),  # 跟_download_one_file返回格式保持一致(download_dir为根)
            "download_status": "skipped",
        })
    return confirmed_skip, need_download


async def _download_with_db_skip(
    session: AsyncSession, folder: dict[str, Any], cli: Any
) -> tuple[list[dict[str, Any]], bool]:
    """跟aliyun_sync._download_with_reconciliation语义一致(核对文件数、
    对不上重试最多RECONCILE_MAX_ATTEMPTS次)，区别是下载前先用
    _split_by_db_cache()筛掉DB里已确认的文件，只对剩下的真正调
    _download_folder_files。reconciliation用的expected_count是原始
    完整文件数(folder["raw_files"])，不是筛掉之后的数量——
    _count_local_files数的是磁盘上实际文件数，包含被跳过的那些，口径
    要对得上。"""
    confirmed_skip, need_download = await _split_by_db_cache(session, folder)
    print(
        f"[urgent_download] DB缓存确认跳过{len(confirmed_skip)}个(内容已验证过，不重新读取)，"
        f"还需下载/验证{len(need_download)}个",
        flush=True,
    )
    expected_count = len(folder["raw_files"])
    downloaded_new: list[dict[str, Any]] = []
    reconciled = False
    for attempt in range(1, RECONCILE_MAX_ATTEMPTS + 1):
        downloaded_new = await _download_folder_files({**folder, "raw_files": need_download}, cli)
        actual_count = _count_local_files(folder, Path(settings.aliyun_download_dir))
        if actual_count == expected_count:
            reconciled = True
            break
        print(
            f"[urgent_download] 文件数核对不上(第{attempt}/{RECONCILE_MAX_ATTEMPTS}次)："
            f"云端{expected_count}个，本地实际{actual_count}个",
            flush=True,
        )
    return confirmed_skip + downloaded_new, reconciled


async def _run_worker(session_name: str, drive_id: str, cloud_folder_id: str) -> None:
    match = SESSION_FOLDER_RE.match(session_name)
    if match is None:
        # 理论上不会发生(前台校验已经挡过一次)，这里只是防御性检查，不是预期路径
        print(f"[urgent_download] 会话名格式不对，worker拒绝执行: {session_name}", flush=True)
        return
    category_folder = derive_category_folder(match)

    cli = _build_cli()
    raw_files = await asyncio.to_thread(_gather_all_files, cli, drive_id, cloud_folder_id)
    folder = {
        "drive_id": drive_id,
        "cloud_folder_id": cloud_folder_id,
        "cloud_folder_path": f"/{session_name}",
        "category_folder": category_folder,
        "session_name": session_name,
        "raw_files": raw_files,
    }

    print(f"[urgent_download] 开始下载: {session_name}，共{len(raw_files)}个文件", flush=True)
    await send_feishu_message(
        f"{PREFIX_ALIYUN} 🚨 紧急下载开始：{session_name}，共{len(raw_files)}个文件。"
        "完成后飞书会提示，长时间没反应请重新尝试下载或联系管理员"
    )

    if not await ensure_enough_space(settings.aliyun_download_dir):
        await send_feishu_message(f"{PREFIX_ALIYUN} ❌ 紧急下载失败：{session_name}，磁盘空间不足")
        return

    scan_dir = settings.aliyun_scan_dir or settings.aliyun_download_dir
    scan_folder = (Path(scan_dir).resolve() / category_folder / session_name)

    async with SessionContext() as session:
        download_started_at = datetime.now(timezone.utc)
        downloaded, reconciled = await _download_with_db_skip(session, folder, cli)
        download_finished_at = datetime.now(timezone.utc)
        all_succeeded = all(d.get("download_status") != "failed" for d in downloaded) and reconciled
        if not all_succeeded:
            failed = [d for d in downloaded if d.get("download_status") == "failed"]
            await send_feishu_message(
                f"{PREFIX_ALIYUN} ❌ 紧急下载失败：{session_name}\n"
                f"文件数核对{'没对上' if not reconciled else '对上了'}，{len(failed)}个文件下载失败"
            )
            print(f"[urgent_download] 失败: {session_name}  reconciled={reconciled}  failed={len(failed)}", flush=True)
            return

        if not _is_within_scan_roots(scan_folder):
            await send_feishu_message(
                f"{PREFIX_ALIYUN} ⚠️ 紧急下载完成，但扫描目录不在AERIAL_SCAN_ROOTS范围内，"
                f"没有自动入库，需要管理员手动处理：{scan_folder}"
            )
            print(f"[urgent_download] 下载完成但不在scan_roots范围内，跳过入库: {scan_folder}", flush=True)
            return

        try:
            await run_scan(session, str(scan_folder))
        except Exception as exc:  # noqa: BLE001  扫描入库这一步绝不能让整个流程静默失败不报错
            await session.rollback()
            await send_feishu_message(
                f"{PREFIX_ALIYUN} ❌ 紧急下载的文件已落盘，但扫描入库失败：{session_name}\n{exc}"
            )
            print(f"[urgent_download] 扫描入库失败: {session_name}  {exc}", flush=True)
            return

        # sha1校验：下载时云端report的content_hash(算法是sha1的那些) vs
        # 扫描入库后videos表里真实算出来的sha1，确认内容没问题才算真正完成——
        # 这是"紧急"模式专门加的一道保险，常规自动化流程(_trigger_download)
        # 只核对文件数量，不校验内容哈希。只对VIDEO_EXTENSIONS这几种视频
        # 扩展名的文件查——SRT遥测字幕文件压根不会被scanner.py扫进videos表
        # (它跟着视频一起下载，但不是视频本身)，拿SRT文件去查videos表
        # 永远查不到，会被误判成"sha1不匹配"(2026-11-05实测踩过：52个文件
        # 里26个SRT全部被误报成不匹配，其实下载内容完全没问题，SRT的正确性
        # 已经由下载那一层的_file_matches()保证过了，不需要再查videos表)
        sha1_expected: dict[str, str] = {
            _to_scan_path(item["local_path"]): item["content_hash"].lower()
            for item in downloaded
            if item.get("content_hash_algo") == "sha1" and item.get("content_hash") and item.get("local_path")
            and Path(item["local_path"]).suffix.lower() in VIDEO_EXTENSIONS
        }
        mismatches: list[tuple[str, str, str | None]] = []
        if sha1_expected:
            result = await session.execute(
                select(Video.storage_path, Video.sha1).where(Video.storage_path.in_(sha1_expected.keys()))
            )
            actual_by_path = {row.storage_path: row.sha1 for row in result}
            for local_path, expected_hex in sha1_expected.items():
                actual = actual_by_path.get(local_path)
                actual_hex = actual.hex() if actual else None
                if actual_hex != expected_hex:
                    mismatches.append((local_path, expected_hex, actual_hex))

        if mismatches:
            lines = "\n".join(
                f"  {p}: 期望{exp[:12]}... 实际{(act or '(空)')[:12]}..." for p, exp, act in mismatches[:5]
            )
            await send_feishu_message(
                f"{PREFIX_ALIYUN} 🚨 紧急下载完整性校验失败：{session_name}，"
                f"{len(mismatches)}个文件sha1对不上，不登记完成，需要人工确认\n{lines}"
            )
            print(f"[urgent_download] sha1校验失败: {session_name}  {len(mismatches)}个不匹配", flush=True)
            return

        session.add(AliyunSyncEvent(
            event_type="folder_ready",
            cloud_folder_id=cloud_folder_id,
            cloud_folder_path=f"/{session_name}",
            videos=downloaded,
            scan_completed_at=datetime.now(timezone.utc),
            download_started_at=download_started_at,
            download_finished_at=download_finished_at,
            notified_at=datetime.now(timezone.utc),
        ))
        await session.commit()

    await send_feishu_message(
        f"{PREFIX_ALIYUN} ✅ 紧急下载完成：{session_name}，共{len(downloaded)}个文件，"
        f"sha1校验通过({len(sha1_expected)}个)，已入库"
    )
    print(f"[urgent_download] 完成: {session_name}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--session-name", required=True,
        help="完整会话文件夹名，格式: city_site_drone_height_person_date",
    )
    parser.add_argument("--worker-mode", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker-drive-id", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--worker-cloud-folder-id", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.worker_mode:
        # 内部用：main()检测到这个标志时，是被前台进程spawn出来的后台worker，
        # 不是用户直接调用的入口，不要在命令行手动传这几个--worker-*参数
        asyncio.run(_run_worker(args.session_name, args.worker_drive_id, args.worker_cloud_folder_id))
        return

    # 第一步：会话名格式完全匹配校验(不是"路径里某一段能匹配上"那种宽松做法)
    if SESSION_FOLDER_RE.match(args.session_name) is None:
        print(f"❌ 会话名格式不对: {args.session_name}")
        print("正确格式: city_site_drone_height_person_date")
        print("例如: shanghai_jianchuanroad_mavic4pro_120m_wangxiangyu_20261104")
        print(f"机型只认: {', '.join(SESSION_FOLDER_DEVICES)}（新机型需要先找管理员加进白名单）")
        print("本次不会下载，请改成正确格式后重试")
        sys.exit(1)

    # 第二步：云端drive根目录下确认真的存在这个文件夹(只查根目录，不递归搜索)
    print(f"正在云端查找 /{args.session_name} ...")
    drive_id, cloud_folder_id = _resolve_at_root(args.session_name)  # 找不到会SystemExit，终端自然报错退出
    print(f"✅ 找到了，drive_id={drive_id}")

    # 第三步：转后台，前台立刻把终端还给用户
    log_dir = Path(__file__).resolve().parent.parent / "urgent_download_logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"{args.session_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    with open(log_path, "wb") as logfile:
        subprocess.Popen(
            [
                sys.executable, str(Path(__file__).resolve()),
                "--session-name", args.session_name,
                "--worker-mode",
                "--worker-drive-id", drive_id,
                "--worker-cloud-folder-id", cloud_folder_id,
            ],
            stdout=logfile, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True,  # 跟终端会话脱钩，终端关掉/SSH断开也不会被杀
        )
    print("✅ 校验通过，已转入后台下载，不再占用终端")
    print(f"后台日志: {log_path}")
    print("完成后飞书会提示；长时间没反应请重新尝试下载或联系管理员")


if __name__ == "__main__":
    main()
