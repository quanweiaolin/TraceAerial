"""给标注团队查"哪些云端会话文件夹现在可以放心开始处理"用的只读接口。

2026-10-28：原来想用一张数据库VIEW实现，但"判定完成后静默多久才算真稳定"
这个阈值要跟settings.aliyun_stability_scans_required联动，写死在SQL里的话
改配置容易忘记同步改视图，而且平白多一个要维护的DB schema对象。改成直接
在这里用Python算这个阈值(读的是运行时的settings，天然跟.env一致，不会
有两份数字对不上的问题)，查询本身用参数化SQL，不在数据库里新建任何东西。

本地路径(local_path)不是存出来的，是现算的：aliyun_sync_events只存了
cloud_folder_path(阿里云那边的路径，不是本地盘上的路径，学生拿着没法直接
用)。本地实际落盘路径 = aliyun_download_dir/category_folder/session_name，
其中session_name就是cloud_folder_path的最后一段(_walk_session_folders里
这两个本来就是同一个字符串)，category_folder是从session_name用
derive_category_folder()确定性算出来的(年份_城市_AerialVideos_季度)——
跟_trigger_download实际下载时用的是同一套计算方式，不会跟真实落盘位置
对不上，也不用另外存一份、担心哪天两边逻辑改了互相脱节。
"""
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.services.aliyun_sync import ALIYUN_SCAN_INTERVAL_HOURS
from app.services.session_naming import SESSION_FOLDER_RE, derive_category_folder

router = APIRouter(prefix="/aliyun", tags=["aliyun"])


def _local_path_for(cloud_folder_path: str) -> str | None:
    """由cloud_folder_path推算本地实际落盘路径，算不出来(名字不符合会话
    命名规范)返回None——理论上不会发生，能走到folder_ready的文件夹名字
    当初一定匹配过SESSION_FOLDER_RE(不匹配的在_report_invalid_session_name
    那一步就被拦截、根本不会开始下载)，这里兜底只是防御性处理，不代表
    预期会触发。"""
    session_name = Path(cloud_folder_path).name
    match = SESSION_FOLDER_RE.match(session_name)
    if match is None:
        return None
    category_folder = derive_category_folder(match)
    return str(Path(settings.aliyun_download_dir) / category_folder / session_name)

_FOLDERS_READY_QUERY = text("""
    WITH latest_ready AS (
        SELECT DISTINCT ON (cloud_folder_id)
            cloud_folder_id, cloud_folder_path, download_finished_at AS ready_at
        FROM aliyun_sync_events
        WHERE event_type = 'folder_ready' AND download_finished_at IS NOT NULL
        ORDER BY cloud_folder_id, download_finished_at DESC
    ),
    latest_issue AS (
        SELECT cloud_folder_id, max(notified_at) AS last_issue_at
        FROM aliyun_sync_events
        -- local_files_missing(本地文件真丢了)跟另外两个一样是实打实的数据
        -- 问题，2026-11-02加进来一起算"不稳定"。不包括folder_removed_from_
        -- cloud(下载完成后手动清云端，正常运维流程，不代表数据有问题)和
        -- invalid_session_name(下载前命名校验就拦下来了，根本进不了这里
        -- 的latest_ready集合，不会跟这张表的任何一行有关联)。
        WHERE event_type = 'issue'
            AND issue_type IN ('reupload_after_complete', 'file_deleted', 'local_files_missing')
        GROUP BY cloud_folder_id
    )
    SELECT
        r.cloud_folder_path,
        r.ready_at,
        i.last_issue_at,
        (i.last_issue_at IS NULL OR now() - i.last_issue_at > make_interval(hours => :stable_hours)) AS is_stable
    FROM latest_ready r
    LEFT JOIN latest_issue i USING (cloud_folder_id)
    ORDER BY r.ready_at DESC
""")


@router.get("/folders-ready")
async def list_folders_ready(session: AsyncSession = Depends(get_session)):
    """按"会话文件夹"粒度(不是按地点聚合，同一地点可能有多个文件夹)：
    这个文件夹被aliyun心跳判定下载完成过，而且完成之后要么从没再变动，
    要么最后一次变动(又有新上传/文件被删除/本地文件丢失)距今已经超过
    稳定期——稳定期
    = AERIAL_ALIYUN_STABILITY_SCANS_REQUIRED * 心跳间隔小时数，实时读
    settings，不是写死的数字，改.env配置下次请求就自动生效。"""
    stable_hours = settings.aliyun_stability_scans_required * ALIYUN_SCAN_INTERVAL_HOURS
    result = await session.execute(_FOLDERS_READY_QUERY, {"stable_hours": stable_hours})
    rows = result.mappings().all()
    return {
        "stable_hours_threshold": stable_hours,
        "folders": [
            {
                "cloud_folder_path": r["cloud_folder_path"],
                "local_path": _local_path_for(r["cloud_folder_path"]),
                "ready_at": r["ready_at"],
                "last_issue_at": r["last_issue_at"],
                "is_stable": r["is_stable"],
            }
            for r in rows
        ],
    }


_FOLDERS_PENDING_QUERY = text("""
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
""")


@router.get("/folders-pending")
async def list_folders_pending(session: AsyncSession = Depends(get_session)):
    """跟/folders-ready互补：还没判定下载完成、仍在累计稳定心跳期的文件夹。
    stable_hours是从"最近一次云端真的发生变化"的那次心跳到现在过了多少
    小时(从没变化过就从第一次被发现时算起)——拿这个数跟
    AERIAL_ALIYUN_STABILITY_SCANS_REQUIRED*心跳间隔小时数(也是
    stable_hours_threshold返回的这个值)比一下，就知道大概还要多久会被
    判定完成。"""
    result = await session.execute(_FOLDERS_PENDING_QUERY)
    rows = result.mappings().all()
    stable_hours_threshold = settings.aliyun_stability_scans_required * ALIYUN_SCAN_INTERVAL_HOURS
    return {
        "stable_hours_threshold": stable_hours_threshold,
        "folders": [
            {
                "cloud_folder_path": r["cloud_folder_path"],
                "stable_hours": round(r["stable_hours"], 1) if r["stable_hours"] is not None else None,
            }
            for r in rows
        ],
    }
