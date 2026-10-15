"""
只读预览阿里云PDS当前能识别到的会话文件夹，不下载任何东西、不碰数据库——
几秒钟就能看到结果，用于快速验证命名校验/路径拼接对不对，不用等几百G
下载完才发现问题。

同时做一次"用量对账"：drive总用量经常比"合法会话+命名不合规"加起来大得多
(2026-10-15发现：某企业盘用了1.9TiB，但扫描出来的合法会话只有13.8GiB)，
差额往往是学生把文件/文件夹放进了完全不沾边的目录结构里(不是命名小错误，
是扫描逻辑压根不认识、会当成"中间层目录"一直往下钻、钻到底也找不到会话
文件夹的那种)。这类内容现在额外统计出来，报出具体在哪个云端目录下、
占多大，方便人工决定要不要手动归位。

用法(在aerial_backend目录下)：
    uv run python scripts/preview_aliyun_sessions.py
"""
from collections import deque

from app.config import settings
from app.services.aliyun_sync import _build_cli, _gather_all_files, _list_children
from app.services.session_naming import (
    SESSION_FOLDER_RE,
    derive_category_folder,
    diagnose_session_folder_name,
    looks_like_session_candidate,
)
from pds_sync.core import list_drives


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{n}B"


def _walk_full_drive(cli, drive: dict) -> tuple[list[dict], list[dict], dict[str, dict[str, int]]]:
    """跟aliyun_sync.py的_walk_session_folders逻辑一致地遍历整个drive，但额外
    统计"没有落进任何会话/命名不合规候选文件夹"的散装文件有多大、在哪个
    目录下——那个函数是生产扫描流程在用的，不改它/不复用它，避免这里新增的
    诊断逻辑影响生产扫描行为。

    命名不合规候选这里也顺带recursively拿一下文件列表算总大小(原来那个
    函数发现命中就不再往下探索，不知道里面到底装了多少东西)。

    返回 (合法会话列表, 命名不合规候选列表, 未分类散装文件统计
    {目录路径: {"count":.., "size":..}})。"""
    drive_id = str(drive.get("drive_id") or "")
    queue: deque[tuple[str, str]] = deque([("root", "/")])
    visited = {"root"}
    sessions: list[dict] = []
    invalid_candidates: list[dict] = []
    unmatched: dict[str, dict[str, int]] = {}

    while queue:
        parent_id, parent_path = queue.popleft()
        for item in _list_children(cli, drive_id, parent_id):
            if item.get("type") != "folder":
                bucket = unmatched.setdefault(parent_path, {"count": 0, "size": 0})
                bucket["count"] += 1
                bucket["size"] += int(item.get("size") or 0)
                continue
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
                    "raw_files": _gather_all_files(cli, drive_id, file_id),
                })
            else:
                queue.append((file_id, folder_path))

    return sessions, invalid_candidates, unmatched


def main() -> None:
    cli = _build_cli()
    drives = list_drives(cli, settings.aliyun_spaces)
    print(f"drives: {[(d.get('drive_id'), d.get('space_type')) for d in drives]}\n")

    for drive in drives:
        sessions, invalid_candidates, unmatched = _walk_full_drive(cli, drive)

        session_total = sum(int(f.get("size") or 0) for folder in sessions for f in folder["raw_files"])
        invalid_total = sum(int(f.get("size") or 0) for c in invalid_candidates for f in c["raw_files"])
        unmatched_total = sum(b["size"] for b in unmatched.values())

        print(f"=== drive {drive.get('drive_id')}：{len(sessions)} 个合法会话，"
              f"{len(invalid_candidates)} 个命名不合规 ===\n")

        for folder in sessions:
            size = sum(int(f.get("size") or 0) for f in folder["raw_files"])
            local = f"{settings.aliyun_download_dir}/{folder['category_folder']}/{folder['session_name']}"
            print(f"[OK] {folder['cloud_folder_path']}")
            print(f"     -> 本地: {local}")
            print(f"     文件数: {len(folder['raw_files'])}  总大小: {_human_bytes(size)}")
            for f in sorted(folder["raw_files"], key=lambda x: x.get("relative_path") or "")[:5]:
                print(f"       - {f.get('relative_path')}  ({_human_bytes(int(f.get('size') or 0))})")
            if len(folder["raw_files"]) > 5:
                print(f"       ...还有 {len(folder['raw_files']) - 5} 个文件")
            print()

        for candidate in invalid_candidates:
            reasons = diagnose_session_folder_name(candidate["name"])
            size = sum(int(f.get("size") or 0) for f in candidate["raw_files"])
            print(f"[命名不合规] {candidate['cloud_folder_path']}  "
                  f"(文件数: {len(candidate['raw_files'])}  总大小: {_human_bytes(size)})")
            for r in reasons:
                print(f"     - {r}")
            print()

        if unmatched:
            print(f"=== 未落入任何会话/候选文件夹的散装文件，共 {_human_bytes(unmatched_total)} ===\n")
            for path, bucket in sorted(unmatched.items(), key=lambda kv: -kv[1]["size"]):
                print(f"[未分类] {path}  文件数: {bucket['count']}  大小: {_human_bytes(bucket['size'])}")
            print()

        print(
            f"--- 用量对账：合法会话 {_human_bytes(session_total)}  "
            f"命名不合规 {_human_bytes(invalid_total)}  "
            f"未分类散装文件 {_human_bytes(unmatched_total)}  "
            f"合计 {_human_bytes(session_total + invalid_total + unmatched_total)} ---\n"
        )


if __name__ == "__main__":
    main()
