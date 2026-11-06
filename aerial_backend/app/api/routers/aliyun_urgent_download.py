"""给不想SSH上服务器、只想发个curl请求的人用的"紧急下载"HTTP入口。

跟scripts/urgent_download.py是同一套校验+spawn后台子进程的逻辑(都在
app/services/urgent_download.py里)，这里只是薄薄一层HTTP包装——请求本身
只做"校验+起后台进程"这两步(几秒钟)，真正的下载/入库在独立子进程里跑，
不会占住这个HTTP请求、也不会占住FastAPI的事件循环。

2026-11-05跟用户确认：当前阶段不加鉴权(实验室内部人数可控)，但输入侧的
校验要做足——完整安全设计见app/services/urgent_download.py模块docstring。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from app.services.session_naming import SESSION_FOLDER_DEVICES, SESSION_FOLDER_RE
from app.services.urgent_download import SessionNotFoundError, resolve_at_root, spawn_background_worker

router = APIRouter(prefix="/aliyun", tags=["aliyun"])


@router.post("/urgent-download")
async def trigger_urgent_download(session_name: str):
    """POST /aliyun/urgent-download?session_name=shanghai_xxx_m3e_120m_someone_20261104

    跟scripts/urgent_download.py命令行版本完全一样的两步校验，校验通过
    就起后台子进程，立刻把HTTP响应返回，不等下载真正跑完——结果(成功/
    失败)靠飞书消息通知，不是这个接口的响应体。"""
    if SESSION_FOLDER_RE.match(session_name) is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"会话名格式不对: {session_name}，应为 city_site_drone_height_person_date，"
                f"机型只认: {', '.join(SESSION_FOLDER_DEVICES)}"
            ),
        )

    try:
        # resolve_at_root内部是阻塞的subprocess调用(走阿里云CLI)，丢进线程池，
        # 不然会卡住FastAPI的事件循环，拖慢同一进程里其他请求(扫盘/飞书agent等)
        drive_id, cloud_folder_id = await asyncio.to_thread(resolve_at_root, session_name)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    log_path = spawn_background_worker(session_name, drive_id, cloud_folder_id)
    return {
        "status": "started",
        "session_name": session_name,
        "drive_id": drive_id,
        "log_path": str(log_path),
        "message": "校验通过，已转入后台下载。完成后飞书会提示；长时间没反应请重新调用本接口或联系管理员",
    }
