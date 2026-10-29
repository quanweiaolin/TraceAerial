"""一次性/按需运行脚本：给已有的videos表记录补算sha1(2026-10-28加的新列，
专门给阿里云下载前查重用——阿里云PDS返回的content_hash字段本身就是sha1，
不是sha256，两种算法不能互相比对，所以要单独存一份)。

只处理 deleted_at IS NULL(还活着) 且 sha1 IS NULL 的记录——已经被瘦身替换、
软删除下线的旧记录，原始字节已经被覆盖，物理上补不出sha1，这个脚本不会碰，
永久跳过，不是bug。

重新读文件算sha1的同时，顺带比对一下算出来的sha256跟DB里存的是否一致——
不一致说明storage_path指向的文件内容已经变了(比如又被瘦身过一次但没走
我们的扫盘流程登记)，这种情况不写sha1、打印出来，需要人工确认，不能盲目
相信"这个路径现在的文件还是当初登记的那份内容"。

视频文件通常几个GB，全量跑完要重新读一遍所有现存视频(可能几十TB)，很慢，
建议配合--limit分批跑(比如每次5000条)，可以重复执行——按sha1 IS NULL
筛选，每次只处理还没补完的，天然可续跑，没有"跑一半中断"的问题。

用法：
    uv run python aerial_backend/scripts/backfill_video_sha1.py \
        --database-url postgresql://user:pass@localhost:5432/aerial_db \
        --limit 5000
    # dry-run(不加--apply)也会真的把每个文件完整读一遍算哈希(这一步本身
    # 没法省，要验证sha256对不对得上就得重新读)，只是算完不写入数据库——
    # 不是"秒出结果"的那种dry-run，耗时跟真跑一样，区别只在最后写不写。
    # 建议先加 --limit 10 (不加--apply)在一小批上确认输出符合预期，
    # 再加大--limit分批正式跑(--apply)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.services.hashing import compute_sha256_and_sha1  # noqa: E402

PROGRESS_EVERY = 50


async def main(database_url: str, apply: bool, limit: int | None) -> None:
    conn = await asyncpg.connect(database_url, server_settings={"search_path": "aerial,public"})
    try:
        query = (
            "SELECT sha256, storage_path FROM videos "
            "WHERE deleted_at IS NULL AND sha1 IS NULL "
            "ORDER BY sha256"
        )
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        rows = await conn.fetch(query)

        total = len(rows)
        print(f"待处理: {total} 条(deleted_at IS NULL且sha1 IS NULL的活记录)")
        if total == 0:
            return

        processed = updated = missing = mismatched = 0
        for row in rows:
            processed += 1
            sha256_db = row["sha256"]
            path = Path(row["storage_path"])

            try:
                sha256_computed, sha1_computed = await asyncio.to_thread(compute_sha256_and_sha1, path)
            except OSError as exc:
                missing += 1
                print(f"[backfill_sha1] 读不到文件，跳过: {path}  {exc}", flush=True)
                continue

            if sha256_computed != sha256_db:
                mismatched += 1
                print(
                    f"[backfill_sha1] 内容对不上，跳过(需要人工确认): {path}  "
                    f"DB记录={sha256_db.hex()[:12]}... 实际文件={sha256_computed.hex()[:12]}...",
                    flush=True,
                )
                continue

            if apply:
                await conn.execute(
                    "UPDATE videos SET sha1 = $1 WHERE sha256 = $2",
                    sha1_computed, sha256_db,
                )
            updated += 1

            if processed % PROGRESS_EVERY == 0:
                print(f"[backfill_sha1] 进度 {processed}/{total}", flush=True)

        print(
            f"\n完成: 共{total}条，{'已更新' if apply else '将更新(dry-run)'} {updated} 条，"
            f"文件缺失跳过 {missing} 条，内容对不上跳过 {mismatched} 条"
        )
        if not apply:
            print("这是dry-run，没有真的写入数据库，确认上面输出没问题后加 --apply 重新跑一遍")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--limit", type=int, default=None, help="本次最多处理几条，建议分批跑(比如5000)，不传就不限制")
    parser.add_argument("--apply", action="store_true", help="不加这个参数只是dry-run(读文件但不写库)，确认没问题后加这个才真的写入")
    args = parser.parse_args()
    asyncio.run(main(args.database_url, args.apply, args.limit))
