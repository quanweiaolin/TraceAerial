"""
对每个当前识别到的会话文件夹，只下载其中最小的那一个文件，用来快速验证
"下载+落盘"这条链路本身没问题(比如权限/chmod这类坑)，不用等几百G的MP4
全部传完才发现问题。跟真实下载走的是完全相同的代码路径(_download_one_file)，
只是只挑一个小文件、不记心跳/不写数据库事件。

用法(在aerial_backend目录下)：
    uv run python scripts/test_aliyun_smallest_file.py
"""
from pathlib import Path

from app.config import settings
from app.services.aliyun_sync import _build_cli, _build_download_cli, _download_one_file, _walk_session_folders
from pds_sync.core import PdsError, list_drives


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{n}B"


def main() -> None:
    cli = _build_cli()
    download_cli = _build_download_cli()
    drives = list_drives(cli, settings.aliyun_spaces)

    for drive in drives:
        sessions, _invalid = _walk_session_folders(cli, drive)
        print(f"drive {drive.get('drive_id')}：{len(sessions)} 个会话待测\n")

        for folder in sessions:
            if not folder["raw_files"]:
                continue
            smallest = min(folder["raw_files"], key=lambda f: int(f.get("size") or 0))
            relative_path = smallest.get("relative_path") or smallest.get("name")
            local_relative = f"{folder['category_folder']}/{folder['session_name']}/{relative_path}"
            size = int(smallest.get("size") or 0)

            print(f"[测试] {folder['cloud_folder_path']}  最小文件: {relative_path} ({_human_bytes(size)})")
            try:
                result = _download_one_file(
                    download_cli, local_relative, smallest, Path(settings.aliyun_download_dir)
                )
                print(f"  成功: status={result['status']}  落地路径={result['local_path']}\n")
            except (PdsError, OSError, ValueError) as exc:
                print(f"  失败: {exc}\n")


if __name__ == "__main__":
    main()
