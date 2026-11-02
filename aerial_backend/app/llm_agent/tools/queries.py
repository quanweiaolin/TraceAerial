"""只读查询工具(场景1 - 日常查询)。

复用app.api.routers.aliyun_status里已经在跑的/folders-ready、
/folders-pending两个接口的SQL和本地路径推算逻辑，不重新写一份——避免
"判定完成"的口径(稳定期阈值等)在两个地方各维护一套，以后改配置漏改
一边。"最近issue"这个查询那边还没有对应的API端点，这里单独写。

2026-11-02按用户要求把原来3个分开的工具(list_folders_ready/
list_folders_pending/list_recent_issues)合并成1个
get_sync_status_report：用户实测发现单独3次工具调用没有直接运行
scripts/aliyun_status_report.py来得直观，那个脚本一次输出"已完成/
还在监控/最近issue"三段完整现状，问一次就够。

"已完成"这段又在Python这边按is_stable直接拆成了completed_stable/
completed_unstable两个列表(而不是一个带is_stable字段的列表)：实测发现
让模型自己拿着is_stable标志在生成文字的同时现算"哪些算成功"，会在复杂
一点的结果里算错(2026-11-02真实报错：模型先把一个is_stable=true的
文件夹错分进了不稳定列表，写到一半才自己发现改口)。这种分类判断是
确定性计算，应该在代码里做好，不该指望模型每次生成文本的同时都推理
正确。

返回JSON字符串(不是dict)：@beta_async_tool不会帮你把dict序列化成
content block，返回裸dict会被Anthropic SDK的tool_result_content()
当成一个可迭代对象去取"key列表"，拼出来的content格式是错的，MiniMax的
校验器会直接拒绝整个请求(2026-11-02实测踩过)。
"""
from __future__ import annotations

import json
from typing import Any

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
    1. completed_stable: 真正下载成功的文件夹(云端路径、本地落盘路径、
       完成时间)——被问"下载成功/完成的有哪些"时直接报这个列表就行
    2. completed_unstable: 虽然判定完成过、但之后又出现过文件丢失/重新
       上传等问题的文件夹，不算成功，想了解细节要看recent_issues
    3. pending: 还在累计稳定心跳、尚未判定完成的文件夹(云端路径、已经
       稳定了多少小时)
    4. recent_issues: 最近出现过异常的文件夹(命名不规范、完成后又上传、
       文件丢失、本地磁盘文件丢失、文件夹从云端整体消失等)，每个文件夹
       只给最新一条，最多20条
    "下载成功/完成的有哪些"、"还有哪些在等"、"最近有什么问题"这类问题
    都用这一个工具，不用分开查。completed_stable/completed_unstable这个
    划分已经在这里分好了，不要自己再按is_stable重新判断一遍——直接照抄
    对应的列表，避免中间重新分类出错。"""
    stable_hours = settings.aliyun_stability_scans_required * ALIYUN_SCAN_INTERVAL_HOURS
    async with SessionContext() as session:
        completed_rows = (
            await session.execute(_FOLDERS_READY_QUERY, {"stable_hours": stable_hours})
        ).mappings().all()
        pending_rows = (await session.execute(_FOLDERS_PENDING_QUERY)).mappings().all()
        issue_rows = (await session.execute(_RECENT_ISSUES_QUERY)).mappings().all()

    def _completed_entry(r: Any) -> dict[str, Any]:
        return {
            "cloud_folder_path": r["cloud_folder_path"],
            "local_path": _local_path_for(r["cloud_folder_path"]),
            "ready_at": str(r["ready_at"]),
        }

    return json.dumps(
        {
            "completed_stable": [_completed_entry(r) for r in completed_rows if r["is_stable"]],
            "completed_unstable": [_completed_entry(r) for r in completed_rows if not r["is_stable"]],
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
