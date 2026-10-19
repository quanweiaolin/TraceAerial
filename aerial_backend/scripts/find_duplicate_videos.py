"""
只读巡检脚本：找出同一份视频内容(sha256相同)同时以≥2份独立物理文件存在于
磁盘上的情况——这种才是真正浪费磁盘空间的"重复下载/重复拷贝"；同一份物理
文件被不同挂载点/bindfs视图(比如个人挂载点迁移到共享挂载点)同时看到、只是
路径字符串不同，不算重复，不占用额外空间，本脚本会自动排除。

(2026-10-19发现：一个会话文件夹因为漏了"分类文件夹"这一层，在磁盘上被
多复制了一份，白白多占16G——当时是手工查scan_discoveries表才发现的，
没有现成脚本，所以写了这个。)

判断逻辑：
  1. 从scan_discoveries表(扫盘时自动写入，不用重新算hash)里找出
     resolved_video_sha256对应了≥2个不同file_path的记录
  2. 对每个路径实际stat()一次：
     - 路径已经不存在(文件已被清理/旧挂载点已失效)的，直接忽略——这种
       不是"现在占了双倍空间"的问题，是历史记录里的过期数据
     - 路径还在的，按(st_dev, st_ino)分组去重——同一个inode只是被不同
       挂载点/bindfs视图看到，只保留一条；分到≥2个不同inode，才是磁盘上
       真实存在的独立拷贝
  3. 用项目现成的session_naming.find_session_match()识别每个路径所在的
     "飞行会话文件夹"，按session分组、以tree格式打印

只读——不删除、不修改任何东西。确认输出没问题后，删除由人自己手动做。

用法：
    uv run python aerial_backend/scripts/find_duplicate_videos.py \
        --database-url postgresql://user:pass@localhost:5432/aerial_db
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.services.session_naming import find_session_match  # noqa: E402


async def fetch_duplicate_groups(conn: asyncpg.Connection) -> list[tuple[bytes, list[str]]]:
    rows = await conn.fetch(
        """
        SELECT resolved_video_sha256, array_agg(DISTINCT file_path) AS paths
        FROM scan_discoveries
        WHERE resolved_video_sha256 IS NOT NULL
        GROUP BY resolved_video_sha256
        HAVING count(DISTINCT file_path) > 1
        """
    )
    return [(r["resolved_video_sha256"], r["paths"]) for r in rows]


def existing_distinct_copies(paths: list[str]) -> list[tuple[str, int]]:
    """只保留磁盘上真实存在的路径，按(st_dev, st_ino)去重——同一个inode(同一份
    物理文件被不同挂载点看到)只保留第一次见到的那个路径。返回[(路径, 大小)]，
    每个物理文件一条。"""
    seen: dict[tuple[int, int], tuple[str, int]] = {}
    for p in paths:
        try:
            st = Path(p).stat()
        except OSError:
            continue
        key = (st.st_dev, st.st_ino)
        seen.setdefault(key, (p, st.st_size))
    return list(seen.values())


def session_name_of(path: str) -> str:
    match = find_session_match(path)
    return match.group(0) if match else str(Path(path).parent)


def split_at_session(path: str, session_name: str) -> tuple[str, str]:
    """把路径从session文件夹那一段切开：copy_root(这份拷贝独有的、session
    文件夹为止的路径，用来区分是磁盘上的哪一份)，relative(session文件夹往
    后的相对路径，用作tree的叶子标签)。"""
    parts = Path(path).parts
    for i, part in enumerate(parts):
        if part == session_name:
            copy_root = str(Path(*parts[: i + 1]))
            relative = str(Path(*parts[i + 1:])) if i + 1 < len(parts) else "."
            return copy_root, relative
    return str(Path(path).parent), Path(path).name


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}P"


async def main(database_url: str) -> None:
    conn = await asyncpg.connect(database_url, server_settings={"search_path": "aerial,public"})
    try:
        groups = await fetch_duplicate_groups(conn)
    finally:
        await conn.close()

    # session_name -> copy_root -> [(relative_path, size)]
    by_session: dict[str, dict[str, list[tuple[str, int]]]] = defaultdict(lambda: defaultdict(list))
    total_wasted = 0
    duplicate_video_count = 0

    for _sha256, paths in groups:
        copies = existing_distinct_copies(paths)
        if len(copies) < 2:
            # 要么已经被清理掉只剩1份，要么本来就是同一份物理文件被多个挂载点
            # 看到(inode相同)——两种情况都不是"当前占了双倍空间"的问题
            continue
        duplicate_video_count += 1
        size = copies[0][1]
        total_wasted += size * (len(copies) - 1)  # 第一份算正常的，其余都是多余拷贝
        session = session_name_of(copies[0][0])
        for path, copy_size in copies:
            copy_root, relative = split_at_session(path, session)
            by_session[session][copy_root].append((relative, copy_size))

    if not by_session:
        print("没有发现任何真正占用了双倍磁盘空间的重复视频。")
        return

    print(
        f"共发现 {len(by_session)} 个会话文件夹存在磁盘重复拷贝，"
        f"{duplicate_video_count} 个视频文件重复，预计可释放 {human_size(total_wasted)} 磁盘空间：\n"
    )

    for session, roots in sorted(by_session.items()):
        session_wasted = sum(size for files in roots.values() for _r, size in files) - max(
            sum(size for _r, size in files) for files in roots.values()
        )
        print(f"📁 {session}  (浪费约 {human_size(session_wasted)})")
        root_items = sorted(roots.items())
        for ri, (copy_root, files) in enumerate(root_items):
            is_last_root = ri == len(root_items) - 1
            print(f"{'└──' if is_last_root else '├──'} {copy_root}/")
            pad = "    " if is_last_root else "│   "
            for fi, (relative, size) in enumerate(sorted(files)):
                is_last_file = fi == len(files) - 1
                print(f"{pad}{'└──' if is_last_file else '├──'} {relative}  ({human_size(size)})")
        print()

    print(f"合计预计可释放磁盘空间: {human_size(total_wasted)}")
    print("以上只是列出来，没有删除任何文件——确认好要保留哪一份之后自己手动删。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args()
    asyncio.run(main(args.database_url))
