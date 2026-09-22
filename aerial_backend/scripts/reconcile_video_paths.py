"""
一次性脚本：视频归档目录做了结构调整——整个"DJI_xxx"叶子文件夹(内容不变，
里面的MP4/SRT文件都没动)被挪到了目录树的另一个位置，不是简单的根目录整体
换名，不能用固定前缀替换storage_path。可能涉及多个根目录(比如
/mnt/Expansion/Aerial_Raw_Videos 挪了根目录本身，/mnt/nas/.../Aerial_Raw_Videos
根目录没动、但内部叶子文件夹一样被挪动了位置)，脚本支持一次传多个root。

因为文件夹内容确认没变，优先按(original_name, file_size_bytes)做快速匹配
(只需要stat()，不用读文件内容)；只有出现同名同大小但对应不上的歧义情况，
才对那份文件重新算sha256来消歧——这样不需要把4646个视频文件全部重新读一遍。

只处理videos.deleted_at IS NULL的活记录——已经被瘦身覆盖软删除的旧记录代表
历史版本，本来就不对应磁盘上现在的文件。

不改动scanner.py，不影响其他表；默认dry-run，确认输出没问题后加--apply才会
真的执行UPDATE。

用法：
    python aerial_backend/scripts/reconcile_video_paths.py \
        /mnt/Expansion/Aerial_Raw_Videos \
        /mnt/nas/ADSafety/ADSafety/Aerial_Raw_Videos \
        --database-url postgresql://user:pass@localhost:5432/aerial_db
    # 确认dry-run输出没问题后加 --apply 真的执行
    # 输出里会按每个root单独列检查文件数，方便确认所有传入的root都真的扫到了
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
from pathlib import Path

import asyncpg

VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv"}
BATCH_SIZE = 200


def compute_sha256(file_path: Path, chunk_size: int = 8 * 1024 * 1024) -> bytes:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.digest()


def iter_video_files(roots: list[Path]):
    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            if path.name.startswith("._") or ".compress_tmp." in path.name:
                continue
            yield root, path


async def resolve_by_hash(conn, path: Path) -> bytes | None:
    """歧义情况(同名同大小对不上/查不到)才走这条路，读整个文件算sha256。"""
    digest = compute_sha256(path)
    row = await conn.fetchrow(
        "SELECT storage_path FROM videos WHERE sha256 = $1 AND deleted_at IS NULL",
        digest,
    )
    return digest if row is not None else None


async def main(roots: list[str], database_url: str, apply: bool) -> None:
    root_paths = [Path(r) for r in roots]
    conn = await asyncpg.connect(database_url, server_settings={"search_path": "aerial,public"})
    try:
        checked = fast_matched = ambiguous = unmatched = updated = 0
        per_root_checked: dict[str, int] = {r: 0 for r in roots}
        batch: list[Path] = []

        async def flush(batch: list[Path]):
            nonlocal fast_matched, ambiguous, unmatched, updated
            if not batch:
                return
            sizes = [p.stat().st_size for p in batch]
            rows = await conn.fetch(
                "SELECT sha256, original_name, file_size_bytes, storage_path FROM videos "
                "WHERE deleted_at IS NULL AND file_size_bytes = ANY($1::bigint[])",
                sizes,
            )
            candidates: dict[tuple[str, int], list[tuple[bytes, str]]] = {}
            for r in rows:
                key = (r["original_name"], r["file_size_bytes"])
                candidates.setdefault(key, []).append((r["sha256"], r["storage_path"]))

            for path, size in zip(batch, sizes):
                new_path = str(path)
                cands = candidates.get((path.name, size), [])
                if len(cands) == 1:
                    digest, old_path = cands[0]
                    fast_matched += 1
                else:
                    # 同名同大小命中0个或多个，走哈希消歧
                    digest = await resolve_by_hash(conn, path)
                    if digest is None:
                        unmatched += 1
                        continue
                    if len(cands) > 1:
                        ambiguous += 1
                    row = await conn.fetchrow(
                        "SELECT storage_path FROM videos WHERE sha256 = $1", digest
                    )
                    old_path = row["storage_path"]

                if old_path == new_path:
                    continue
                print(f"{old_path}  ->  {new_path}")
                if apply:
                    await conn.execute(
                        "UPDATE videos SET storage_path = $1 WHERE sha256 = $2",
                        new_path, digest,
                    )
                    updated += 1

        for root, path in iter_video_files(root_paths):
            checked += 1
            per_root_checked[str(root)] += 1
            batch.append(path)
            if len(batch) >= BATCH_SIZE:
                await flush(batch)
                batch = []
        await flush(batch)

        print("\n各根目录检查文件数:")
        for root, count in per_root_checked.items():
            print(f"  {root}: {count}")

        print(
            f"\n磁盘上检查文件数合计: {checked}  快速匹配(名字+大小): {fast_matched}  "
            f"歧义走哈希消歧: {ambiguous}  DB里找不到对应记录: {unmatched}  "
            f"{'已更新' if apply else '将更新(dry-run)'}: {updated if apply else '见上面列表'}"
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", help="新的视频根目录，可以传多个")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--apply", action="store_true", help="不加这个参数只是dry-run打印，不会真的改库")
    args = parser.parse_args()
    asyncio.run(main(args.roots, args.database_url, args.apply))
