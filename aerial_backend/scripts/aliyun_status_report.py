"""打印阿里云同步流水线当前状态的三张清单：
  1. 已完成下载的文件夹(云端地址、本地落盘地址、下载完成时间)
  2. 还在累计稳定心跳、没判定完成下载的文件夹(云端地址、已经稳定了多少
     小时——从"最近一次云端真的发生变化"的那次心跳算起，没变化过就从
     第一次被发现时算起)
  3. 最近有过issue的文件夹(命名不规范/完成后又上传/文件丢失/本地磁盘
     文件丢失/文件夹从云端整体消失等，每个文件夹只显示最新那一条)

跟API端点/aliyun/folders-ready(已完成)、/aliyun/folders-pending(还在
监控)用的是同一套查询逻辑，这个脚本是给想要一次性看全貌、或者不方便
发HTTP请求时用的命令行版本。

用法：
    uv run python aerial_backend/scripts/aliyun_status_report.py \
        --database-url postgresql://user:pass@localhost:5432/aerial_db \
        --download-dir /mnt/Expansion2-shared/Aerial_Raw_Videos
    # --download-dir要跟.env里AERIAL_ALIYUN_DOWNLOAD_DIR一致，用来算本地
    # 落盘路径(本地路径不是存出来的，是从云端路径+会话命名规范现算的，
    # 跟app/api/routers/aliyun_status.py的_local_path_for同一个算法)。
    # 这里单独传参是为了这个脚本不依赖app.config/.env。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.services.session_naming import SESSION_FOLDER_RE, derive_category_folder  # noqa: E402

_COMPLETED_QUERY = """
    SELECT DISTINCT ON (cloud_folder_id)
        cloud_folder_path, download_finished_at
    FROM aliyun_sync_events
    WHERE event_type = 'folder_ready' AND download_finished_at IS NOT NULL
    ORDER BY cloud_folder_id, download_finished_at DESC
"""

_PENDING_QUERY = """
    WITH ready AS (
        SELECT DISTINCT cloud_folder_id FROM aliyun_sync_events
        WHERE event_type = 'folder_ready' AND download_finished_at IS NOT NULL
    ),
    last_change AS (
        SELECT DISTINCT ON (cloud_folder_id) cloud_folder_id, scanned_at AS changed_at
        FROM aliyun_folder_scans WHERE changed = true
        ORDER BY cloud_folder_id, scanned_at DESC
    ),
    first_seen AS (
        SELECT cloud_folder_id, min(scanned_at) AS first_seen_at
        FROM aliyun_folder_scans GROUP BY cloud_folder_id
    ),
    latest_scan AS (
        SELECT DISTINCT ON (cloud_folder_id) cloud_folder_id, cloud_folder_path, scanned_at
        FROM aliyun_folder_scans ORDER BY cloud_folder_id, scanned_at DESC
    )
    SELECT
        s.cloud_folder_path,
        EXTRACT(EPOCH FROM (now() - COALESCE(c.changed_at, f.first_seen_at))) / 3600 AS stable_hours
    FROM latest_scan s
    JOIN first_seen f USING (cloud_folder_id)
    LEFT JOIN last_change c USING (cloud_folder_id)
    WHERE s.cloud_folder_id NOT IN (SELECT cloud_folder_id FROM ready)
    ORDER BY stable_hours DESC
"""

_ISSUES_QUERY = """
    SELECT DISTINCT ON (cloud_folder_id)
        cloud_folder_path, issue_type, created_at
    FROM aliyun_sync_events
    WHERE event_type = 'issue'
    ORDER BY cloud_folder_id, created_at DESC
"""


def _local_path_for(cloud_folder_path: str, download_dir: str) -> str | None:
    """跟app/api/routers/aliyun_status.py的_local_path_for同一个算法，这里
    复制一份而不是共享，避免给这个独立脚本强行拉一条对app.config的依赖链
    (那边的版本读的是settings.aliyun_download_dir，这里改成显式CLI参数)。"""
    session_name = Path(cloud_folder_path).name
    match = SESSION_FOLDER_RE.match(session_name)
    if match is None:
        return None
    category_folder = derive_category_folder(match)
    return str(Path(download_dir) / category_folder / session_name)


async def main(database_url: str, download_dir: str) -> None:
    conn = await asyncpg.connect(database_url, server_settings={"search_path": "aerial,public"})
    try:
        completed = await conn.fetch(_COMPLETED_QUERY)
        print(f"===已完成下载({len(completed)}个)===")
        for row in completed:
            local_path = _local_path_for(row["cloud_folder_path"], download_dir)
            print(f"  云端: {row['cloud_folder_path']}")
            print(f"    本地: {local_path}")
            print(f"    完成时间: {row['download_finished_at']}")

        pending = await conn.fetch(_PENDING_QUERY)
        print(f"\n===还在监控中，未判定完成下载({len(pending)}个)===")
        for row in pending:
            hours = row["stable_hours"]
            print(f"  云端: {row['cloud_folder_path']}  已稳定 {hours:.1f} 小时" if hours is not None else f"  云端: {row['cloud_folder_path']}")

        issues = await conn.fetch(_ISSUES_QUERY)
        print(f"\n===最近有过issue的文件夹({len(issues)}个，每个只显示最新一条)===")
        for row in issues:
            print(f"  云端: {row['cloud_folder_path']}  issue_type={row['issue_type']}  时间={row['created_at']}")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True)
    parser.add_argument(
        "--download-dir", required=True,
        help="本地下载根目录，要跟.env里AERIAL_ALIYUN_DOWNLOAD_DIR一致，用来算本地落盘路径",
    )
    args = parser.parse_args()
    asyncio.run(main(args.database_url, args.download_dir))
