"""只读查询工具(场景1 - 日常查询)。

复用app.api.routers.aliyun_status里已经在跑的/folders-ready、
/folders-pending两个接口的SQL和本地路径推算逻辑，不重新写一份——避免
"判定完成"的口径(稳定期阈值等)在两个地方各维护一套，以后改配置漏改
一边。"最近issue"这个查询那边还没有对应的API端点，这里单独写。

每个工具函数返回JSON字符串(不是dict)：@beta_async_tool不会帮你把
dict序列化成content block，返回裸dict会被Anthropic SDK的
tool_result_content()当成一个可迭代对象去取"key列表"，拼出来的
content格式是错的，MiniMax的校验器会直接拒绝整个请求(2026-11-02实测
踩过)。
"""
from __future__ import annotations

import json

from anthropic import beta_async_tool
from sqlalchemy import text

from app.api.routers.aliyun_status import (
    ALIYUN_SCAN_INTERVAL_HOURS,
    _FOLDERS_PENDING_QUERY,
    _FOLDERS_READY_QUERY,
    _local_path_for,
)
from app.config import settings
from app.db import SessionContext

_RECENT_ISSUES_QUERY = text("""
    SELECT DISTINCT ON (cloud_folder_id)
        cloud_folder_path, issue_type, created_at
    FROM aliyun_sync_events
    WHERE event_type = 'issue'
    ORDER BY cloud_folder_id, created_at DESC
    LIMIT 20
""")


@beta_async_tool
async def list_folders_ready() -> str:
    """查询已经判定下载完成、可以放心开始处理的航测视频文件夹列表。
    返回每个文件夹的云端路径、本地落盘路径、完成时间，以及是否稳定
    (完成后又出现过文件丢失/重新上传就不稳定)。"""
    stable_hours = settings.aliyun_stability_scans_required * ALIYUN_SCAN_INTERVAL_HOURS
    async with SessionContext() as session:
        result = await session.execute(_FOLDERS_READY_QUERY, {"stable_hours": stable_hours})
        rows = result.mappings().all()
    return json.dumps(
        [
            {
                "cloud_folder_path": r["cloud_folder_path"],
                "local_path": _local_path_for(r["cloud_folder_path"]),
                "ready_at": str(r["ready_at"]),
                "is_stable": r["is_stable"],
            }
            for r in rows
        ],
        ensure_ascii=False,
    )


@beta_async_tool
async def list_folders_pending() -> str:
    """查询还在累计稳定心跳、尚未判定下载完成的航测视频文件夹列表，
    附带每个文件夹已经稳定了多少小时(跟判定完成所需的稳定期阈值比一下，
    就知道大概还要多久会被判定完成)。"""
    async with SessionContext() as session:
        result = await session.execute(_FOLDERS_PENDING_QUERY)
        rows = result.mappings().all()
    return json.dumps(
        [
            {
                "cloud_folder_path": r["cloud_folder_path"],
                "stable_hours": round(r["stable_hours"], 1) if r["stable_hours"] is not None else None,
            }
            for r in rows
        ],
        ensure_ascii=False,
    )


@beta_async_tool
async def list_recent_issues() -> str:
    """查询最近出现过异常的文件夹(命名不规范、完成后又上传、文件丢失、
    本地磁盘文件丢失、文件夹从云端整体消失等)，每个文件夹只返回最新
    一条issue，最多20条。"""
    async with SessionContext() as session:
        result = await session.execute(_RECENT_ISSUES_QUERY)
        rows = result.mappings().all()
    return json.dumps(
        [
            {
                "cloud_folder_path": r["cloud_folder_path"],
                "issue_type": r["issue_type"],
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ],
        ensure_ascii=False,
    )


QUERY_TOOLS = [list_folders_ready, list_folders_pending, list_recent_issues]
