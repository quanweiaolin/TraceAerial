"""手动从阿里云PDS下载指定云端路径(文件夹或单个文件)到本地，按现有系统的
本地落盘规则(category_folder/session_name/相对路径，跟app.services.
aliyun_sync.py常规自动下载走的是同一套路径拼接逻辑)。

专门给"发现云端有、本地NAS没有"这种情况做手动补救用——2026-10-29真实
事故：一个session文件夹本地整理时丢了36个MP4，aliyun_sync.py现在的
本地完整性检查只会报警(飞书+log)，不会自动重新下载(自动修复有风险：
NAS挂载短暂抖动可能让大批文件夹同时被误判成"丢了"，自动下载就是一次
误判引发的下载风暴；而且"修复本地磁盘完整性"也不该是"同步云端新内容"
那个模块的职责)。发现问题之后，人工确认真的需要恢复，用这个脚本接着做。

云端路径(--cloud-path)必须包含一段符合会话命名规范的文件夹名
(city_site_drone_height_person_date，跟SESSION_FOLDER_RE一致)，用来
算出本地应该落盘到哪个分类目录——可以直接给整个会话文件夹，也可以给它
下面更深的子目录(比如某一个DJI_xxx)或者单个文件，脚本会自己找到路径里
哪一段是会话文件夹名。

这个脚本故意不碰数据库(不扫描入库、不查/写videos表)——下载下来的文件
跟原来丢失前是同一份内容(同sha256)，DB里的videos行本来就还在(只是
storage_path对应的物理文件丢了)，不需要重新入库；如果想确认恢复后的
文件内容/哈希跟DB记录一致，用现有的main.py扫盘或者backfill_video_sha1.py
验证即可，不是这个脚本的职责。

用法：
    uv run python aerial_backend/scripts/download_from_aliyun.py \
        --cloud-path "/changchun_minzhuprimaryschoolyataiexpressway_m3e_120m_liaokaishen_20261019" \
        --download-dir /mnt/Expansion2-shared/Aerial_Raw_Videos
    # 不加--apply是dry-run：只解析云端路径、列出会下载哪些文件、本地落盘
    # 到哪、每个文件多大，只print，不真的下载也不发飞书。确认没问题后加
    # --apply才真正下载，下载完成后发一条飞书消息(同时也print，跟dry-run
    # 的区别只在这里)。
    #
    # --drive-id不传会自动在--spaces(默认personal,team,enterprise)里
    # 挨个找这个路径存在于哪个drive；如果有歧义(多个drive都有这个路径，
    # 极少见)会报错要你自己指定。
    #
    # --feishu-webhook-url不传就读环境变量AERIAL_FEISHU_WEBHOOK_URL；
    # 两边都没有就跳过发飞书，只print(不是dry-run也一样，不会报错)。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
import secrets
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # aerial_backend/
from app.services.session_naming import SESSION_FOLDER_RE, derive_category_folder  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
PDS_TOOL_ROOT = REPO_ROOT / "extern" / "pds-drive-sync"
if str(PDS_TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(PDS_TOOL_ROOT))

from pds_sync.core import (  # noqa: E402  (必须在sys.path插入之后才能import)
    ITEM_PROJECTION, PdsCli, PdsError, _conflict_path, _file_matches, _paginate, list_drives,
)

PREFIX_ALIYUN = "【阿里云下载】"  # 跟app.services.aliyun_sync.py的飞书消息前缀保持一致
RESOLVE_PROJECTION = (
    "file.{type:type,size:size,content_hash:content_hash,"
    "content_hash_name:content_hash_name,file_id:file_id,name:name}"
)


def _gen_sign(timestamp: int, secret: str) -> str:
    """飞书自定义机器人签名算法，跟app.services.notifier._gen_sign完全一致——
    这个脚本故意不import那个模块(会连带把app.config也import进来，这个脚本
    本身不碰数据库，不想额外要求AERIAL_DATABASE_URL这个跟它无关的环境变量)。"""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def send_feishu_message(text: str, webhook_url: str, webhook_secret: str) -> bool:
    if not webhook_url:
        return False
    payload: dict = {"msg_type": "text", "content": {"text": text}}
    if webhook_secret:
        timestamp = int(time.time())
        payload["timestamp"] = str(timestamp)
        payload["sign"] = _gen_sign(timestamp, webhook_secret)
    resp = httpx.post(webhook_url, json=payload, timeout=10)
    return resp.status_code == 200


def _resolve_local_prefix(cloud_path: str) -> tuple[str, str, str]:
    """在云端路径里找符合会话命名规范的那一段，算出本地分类目录+会话文件夹
    名+该段之后剩余的相对路径(POSIX风格，没有剩余就是空字符串)。"""
    parts = [p for p in cloud_path.split("/") if p]
    for i, part in enumerate(parts):
        match = SESSION_FOLDER_RE.match(part)
        if match:
            category_folder = derive_category_folder(match)
            after = "/".join(parts[i + 1:])
            return category_folder, part, after
    raise SystemExit(
        f"云端路径里找不到符合会话命名规范的那一段(city_site_drone_height_person_date): {cloud_path}"
    )


def _find_drive_for_path(cli: PdsCli, spaces: list[str], cloud_path: str) -> tuple[str, dict[str, Any]]:
    """--drive-id没传时，在spaces覆盖的全部drive里挨个试resolve-path，
    找出这个路径实际存在于哪个drive。返回(drive_id, file信息dict)。"""
    drives = list_drives(cli, spaces)
    matches: list[tuple[str, dict[str, Any]]] = []
    for drive in drives:
        drive_id = str(drive.get("drive_id") or "")
        try:
            file_info = cli.pds([
                "resolve-path", "--drive-id", drive_id, "--path", cloud_path,
                "--cli-query", RESOLVE_PROJECTION,
            ])
        except PdsError:
            continue
        if isinstance(file_info, dict) and file_info.get("file_id"):
            matches.append((drive_id, file_info))
    if not matches:
        raise SystemExit(f"在可访问的全部drive({[d.get('drive_id') for d in drives]})里都找不到这个路径: {cloud_path}")
    if len(matches) > 1:
        found = [m[0] for m in matches]
        raise SystemExit(f"这个路径在多个drive里都存在({found})，有歧义，请用--drive-id指定其中一个")
    return matches[0]


def _list_children(cli: PdsCli, drive_id: str, parent_id: str) -> list[dict[str, Any]]:
    return _paginate(
        cli,
        ["list-file", "--drive-id", drive_id, "--parent-file-id", parent_id, "--limit", "100",
         "--cli-query", ITEM_PROJECTION],
    )


def _gather_all_files(cli: PdsCli, drive_id: str, start_file_id: str) -> list[dict[str, Any]]:
    """递归收集一个文件夹下全部文件(不管嵌套多深)，每个文件附带relative_path
    (相对这个起始文件夹的相对路径)——跟app.services.aliyun_sync._gather_all_files
    是同一套逻辑，这个脚本故意不import那个模块(见模块docstring)，这里复制
    一份而不是共享，避免给这个独立小脚本强行拉一条对app.config的依赖链。"""
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


def _download_one_file(
    cli: PdsCli, drive_id: str, local_relative_path: str, raw_item: dict[str, Any], download_root: Path
) -> dict[str, Any]:
    """跟app.services.aliyun_sync._download_one_file同一套安全逻辑(临时文件+
    fsync+原子替换+按哈希跳过已存在且内容对的文件+冲突改名)，复制一份而不是
    共享(理由同_gather_all_files)。"""
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
        cli.pds([
            "download-to-local", "--drive-id", drive_id, "--file-id", file_id, "--save-to", str(temporary),
        ])
        if not temporary.is_file():
            raise PdsError(f"download did not create a local file for {file_id}")
        local_size = temporary.stat().st_size
        expected = raw_item.get("size")
        if isinstance(expected, int) and expected >= 0 and local_size != expected:
            raise PdsError(f"download size verification failed for {file_id}: expected {expected}, got {local_size}")
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        return {"file_id": file_id, "local_path": str(target), "size": local_size, "status": status}
    finally:
        if temporary.exists():
            temporary.unlink()


def main(args: argparse.Namespace) -> None:
    cli = PdsCli(Path(args.aliyun_cli_binary), timeout_seconds=args.timeout_seconds)
    spaces = [s.strip() for s in args.spaces.split(",") if s.strip()]

    if args.drive_id:
        file_info = cli.pds([
            "resolve-path", "--drive-id", args.drive_id, "--path", args.cloud_path,
            "--cli-query", RESOLVE_PROJECTION,
        ])
        drive_id = args.drive_id
    else:
        drive_id, file_info = _find_drive_for_path(cli, spaces, args.cloud_path)

    category_folder, session_name, after = _resolve_local_prefix(args.cloud_path)
    print(f"drive_id={drive_id}  类型={file_info.get('type')}")
    print(f"本地分类目录={category_folder}  会话文件夹={session_name}  会话内相对路径={after or '(就是会话文件夹本身)'}")

    plan: list[tuple[str, dict[str, Any]]] = []  # (local_relative_path, raw_item)
    if file_info.get("type") == "file":
        local_relative = f"{category_folder}/{session_name}/{after}" if after else f"{category_folder}/{session_name}"
        plan.append((local_relative, file_info))
    else:
        raw_files = _gather_all_files(cli, drive_id, str(file_info.get("file_id") or ""))
        for item in raw_files:
            suffix = f"{after}/{item['relative_path']}" if after else item["relative_path"]
            local_relative = f"{category_folder}/{session_name}/{suffix}"
            plan.append((local_relative, item))

    total_size = sum(int(item.get("size") or 0) for _, item in plan)
    print(f"\n共{len(plan)}个文件，总大小{total_size / (1024 ** 3):.2f} GiB")
    for local_relative, item in plan:
        size_mb = int(item.get("size") or 0) / (1024 ** 2)
        print(f"  {args.download_dir}/{local_relative}  ({size_mb:.1f} MiB)  <- {item.get('name')}")

    if not args.apply:
        print("\n这是dry-run，没有真的下载，也不会发飞书。确认上面列表没问题后加 --apply 重新跑一遍")
        return

    download_root = Path(args.download_dir)
    results = []
    for local_relative, item in plan:
        try:
            result = _download_one_file(cli, drive_id, local_relative, item, download_root)
        except (PdsError, OSError, ValueError) as exc:
            print(f"[download_from_aliyun]   failed     {item.get('name')}  error={exc}", flush=True)
            results.append({"name": item.get("name"), "status": "failed", "error": str(exc)})
            continue
        print(f"[download_from_aliyun]   {result['status']:<10} {item.get('name')} -> {result['local_path']}", flush=True)
        results.append({"name": item.get("name"), "status": result["status"]})

    new_count = sum(1 for r in results if r["status"] == "downloaded")
    skipped_count = sum(1 for r in results if r["status"] == "skipped")
    conflicted_count = sum(1 for r in results if r["status"] == "conflicted")
    failed_count = sum(1 for r in results if r["status"] == "failed")
    summary = (
        f"新下载 {new_count} 个，已存在跳过 {skipped_count} 个，冲突 {conflicted_count} 个，"
        f"失败 {failed_count} 个，共 {len(results)} 个文件"
    )
    message = f"{PREFIX_ALIYUN} 🔧 手动补下载完成：{args.cloud_path}\n{summary}"
    print(f"\n{message}")

    webhook_url = args.feishu_webhook_url or os.environ.get("AERIAL_FEISHU_WEBHOOK_URL", "")
    webhook_secret = args.feishu_webhook_secret or os.environ.get("AERIAL_FEISHU_WEBHOOK_SECRET", "")
    if webhook_url:
        sent = send_feishu_message(message, webhook_url, webhook_secret)
        print("(飞书已发送)" if sent else "(飞书发送失败，上面消息内容已打印，可以自己手动发)")
    else:
        print("(没有配置飞书webhook，跳过发送，只print)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cloud-path", required=True, help='阿里云PDS云端路径，比如 "/changchun_xxx_m3e_120m_someone_20261019"')
    parser.add_argument("--download-dir", required=True, help="本地下载根目录，要跟settings.aliyun_download_dir一致")
    parser.add_argument("--drive-id", default=None, help="不传就自动在--spaces范围内找这个路径存在于哪个drive")
    parser.add_argument("--spaces", default="personal,team,enterprise", help="--drive-id不传时搜索的空间类型，逗号分隔")
    parser.add_argument(
        "--aliyun-cli-binary", default=str(PDS_TOOL_ROOT / ".tools" / "aliyun"),
        help="aliyun CLI二进制路径，默认跟settings.aliyun_cli_binary的默认值一致",
    )
    parser.add_argument("--timeout-seconds", type=int, default=1800, help="单个文件下载超时(秒)，默认1800")
    parser.add_argument("--feishu-webhook-url", default=None, help="不传就读环境变量AERIAL_FEISHU_WEBHOOK_URL")
    parser.add_argument("--feishu-webhook-secret", default=None, help="不传就读环境变量AERIAL_FEISHU_WEBHOOK_SECRET")
    parser.add_argument("--apply", action="store_true", help="不加这个参数只是dry-run(只列清单，不下载不发飞书)，确认没问题后加这个才真的下载")
    main(parser.parse_args())
