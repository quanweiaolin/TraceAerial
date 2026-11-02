"""只读查询工具(场景1 - 日常查询)。

复用app.api.routers.aliyun_status里已经在跑的/folders-ready、
/folders-pending两个接口的SQL和本地路径推算逻辑，不重新写一份——避免
"判定完成"的口径(稳定期阈值等)在两个地方各维护一套，以后改配置漏改
一边。"最近issue"这个查询那边还没有对应的API端点，这里单独写。

2026-11-02按用户要求把原来3个分开的工具(list_folders_ready/
list_folders_pending/list_recent_issues)合并成1个
get_sync_status_report：用户实测发现单独3次工具调用没有直接运行
scripts/aliyun_status_report.py来得直观，那个脚本一次输出"已完成/
还在监控/最近issue"三段完整现状，问一次就够——合并后结构照着那个脚本
的三段对齐，但保留了is_stable字段(脚本本身没有，是这次会话才加的
"成功/不稳定"区分，继续留着)。

返回JSON字符串(不是dict)：@beta_async_tool不会帮你把dict序列化成
content block，返回裸dict会被Anthropic SDK的tool_result_content()
当成一个可迭代对象去取"key列表"，拼出来的content格式是错的，MiniMax的
校验器会直接拒绝整个请求(2026-11-02实测踩过)。
"""
from __future__ import annotations

import json

from anthropic import beta_async_tool

from app.api.routers.aliyun_status import (
    ALIYUN_SCAN_INTERVAL_HOURS,
    _FOLDERS_PENDING_QUERY,
    _FOLDERS_READY_QUERY,
    _RECENT_ISSUES_QUERY,
    _local_path_for,
)
from app.config import settings
from app.db import SessionContext


@beta_async_tool
async def get_sync_status_report() -> str:
    """查询阿里云同步流水线的完整现状，一次返回三段：
    1. completed: 已判定下载完成的文件夹(云端路径、本地落盘路径、完成
       时间、是否稳定——完成后又出现过文件丢失/重新上传就不稳定，不稳定
       的不应该算"成功")
    2. pending: 还在累计稳定心跳、尚未判定完成的文件夹(云端路径、已经
       稳定了多少小时)
    3. recent_issues: 最近出现过异常的文件夹(命名不规范、完成后又上传、
       文件丢失、本地磁盘文件丢失、文件夹从云端整体消失等)，每个文件夹
       只给最新一条，最多20条
    "下载成功/完成的有哪些"、"还有哪些在等"、"最近有什么问题"这类问题
    都用这一个工具，不用分开查。"""
    stable_hours = settings.aliyun_stability_scans_required * ALIYUN_SCAN_INTERVAL_HOURS
    async with SessionContext() as session:
        completed_rows = (
            await session.execute(_FOLDERS_READY_QUERY, {"stable_hours": stable_hours})
        ).mappings().all()
        pending_rows = (await session.execute(_FOLDERS_PENDING_QUERY)).mappings().all()
        issue_rows = (await session.execute(_RECENT_ISSUES_QUERY)).mappings().all()

    return json.dumps(
        {
            "completed": [
                {
                    "cloud_folder_path": r["cloud_folder_path"],
                    "local_path": _local_path_for(r["cloud_folder_path"]),
                    "ready_at": str(r["ready_at"]),
                    "is_stable": r["is_stable"],
                }
                for r in completed_rows
            ],
            "pending": [
                {
                    "cloud_folder_path": r["cloud_folder_path"],
                    # EXTRACT(...)/3600在这条SQL里实测会被asyncpg当成
                    # decimal.Decimal返回(跟collect_lon/collect_lat那次
                    # 踩过的坑一样)，先float()再round()，不然json.dumps
                    # 直接报TypeError(2026-11-02生产实测报错)
                    "stable_hours": round(float(r["stable_hours"]), 1) if r["stable_hours"] is not None else None,
                }
                for r in pending_rows
            ],
            "recent_issues": [
                {
                    "cloud_folder_path": r["cloud_folder_path"],
                    "issue_type": r["issue_type"],
                    "created_at": str(r["created_at"]),
                }
                for r in issue_rows
            ],
        },
        ensure_ascii=False,
        default=str,  # 兜底：以后再出现什么没转好的类型(Decimal/UUID等)，转成
                      # 字符串也比直接把整个工具调用崩掉强
    )


QUERY_TOOLS = [get_sync_status_report]
