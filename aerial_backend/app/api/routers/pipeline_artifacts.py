"""下游各stage(感知跟踪处理/数据集生成/场景提取等，各自独立仓库、按需更新)
登记自己产出了什么的入口。设计见
docs/superpowers/specs/2026-11-16-pipeline-artifact-provenance-design.md。

调用方只传原始视频的绝对路径，这里按`videos.storage_path`反查sha256——
调用方不用自己对大视频文件算哈希。路径必须全部能查到才算成功(整体422，
不允许部分匹配)，避免登记内容跟实际入库数据脱节。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import PipelineArtifact, Video

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


class PipelineArtifactIn(BaseModel):
    stage_code: str
    input_video_paths: list[str]
    output_path: str
    code_repo: str | None = None
    code_commit: str | None = None
    code_dirty: bool | None = None
    metadata: dict = {}
    operator_name: str | None = None


@router.post("/artifacts")
async def register_pipeline_artifact(
    body: PipelineArtifactIn,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Video.storage_path, Video.sha256).where(
            Video.storage_path.in_(body.input_video_paths)
        )
    )
    path_to_sha256 = {path: sha256 for path, sha256 in result.all()}

    missing_paths = [p for p in body.input_video_paths if p not in path_to_sha256]
    if missing_paths:
        raise HTTPException(
            status_code=422,
            detail=f"以下路径在videos表里查不到: {missing_paths}",
        )

    artifact = PipelineArtifact(
        stage_code=body.stage_code,
        input_video_sha256=[path_to_sha256[p] for p in body.input_video_paths],
        code_repo=body.code_repo,
        code_commit=body.code_commit,
        code_dirty=body.code_dirty,
        output_path=body.output_path,
        metadata_=body.metadata,
        operator_name=body.operator_name,
    )
    session.add(artifact)
    await session.commit()

    return {
        "id": artifact.id,
        "input_video_sha256": [h.hex() for h in artifact.input_video_sha256],
    }
