"""给标注团队查"哪些云端会话文件夹现在可以放心开始处理"用的只读接口。

2026-10-28：原来想用一张数据库VIEW实现，但"判定完成后静默多久才算真稳定"
这个阈值要跟settings.aliyun_stability_scans_required联动，写死在SQL里的话
改配置容易忘记同步改视图，而且平白多一个要维护的DB schema对象。改成直接
在这里用Python算这个阈值(读的是运行时的settings，天然跟.env一致，不会
有两份数字对不上的问题)，查询本身用参数化SQL，不在数据库里新建任何东西。
"""
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.services.aliyun_sync import ALIYUN_SCAN_INTERVAL_HOURS

router = APIRouter(prefix="/aliyun", tags=["aliyun"])

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
        WHERE event_type = 'issue' AND issue_type IN ('reupload_after_complete', 'file_deleted')
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
    要么最后一次变动(又有新上传/文件被删除)距今已经超过稳定期——稳定期
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
                "ready_at": r["ready_at"],
                "last_issue_at": r["last_issue_at"],
                "is_stable": r["is_stable"],
            }
            for r in rows
        ],
    }
