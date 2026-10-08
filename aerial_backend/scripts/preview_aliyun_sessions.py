"""
只读预览阿里云PDS当前能识别到的会话文件夹，不下载任何东西、不碰数据库——
几秒钟就能看到结果，用于快速验证命名校验/路径拼接对不对，不用等几百G
下载完才发现问题。

用法(在aerial_backend目录下)：
    uv run python scripts/preview_aliyun_sessions.py
"""
from app.config import settings
from app.services.aliyun_sync import _build_cli, _walk_session_folders
from app.services.session_naming import diagnose_session_folder_name
from pds_sync.core import list_drives


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{n}B"


def main() -> None:
    cli = _build_cli()
    drives = list_drives(cli, settings.aliyun_spaces)
    print(f"drives: {[(d.get('drive_id'), d.get('space_type')) for d in drives]}\n")

    for drive in drives:
        sessions, invalid_candidates = _walk_session_folders(cli, drive)

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
            print(f"[命名不合规] {candidate['cloud_folder_path']}")
            for r in reasons:
                print(f"     - {r}")
            print()


if __name__ == "__main__":
    main()
