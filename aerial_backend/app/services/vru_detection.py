"""
VRU(弱势道路使用者)检测QA可视化流程。整合自学生原脚本(detect_expansion_vru.py)，
原脚本之后不再单独使用，逻辑并入这里统一维护。

帧选取逻辑原样保留学生原脚本的写法(random.seed(SEED)只调一次，按视频顺序
依次randint消耗随机数)——这是为了能复现学生那边已经跑过的结果，不要在这里
"优化"成每视频独立种子之类的写法，会导致跟已有结果对不上。

流程：
  1. select_candidate_videos: 从videos表按条件(默认"最新collect日期")挑候选，
     过滤掉Processing目录下已经有Test文件夹的(幂等，不重复生成)
  2. run_detection_batch: 对候选视频抽帧+YOLO检测，存图到各自Test/目录，
     写一份批次清单json，前端靠这个清单知道"这批有哪些"，不用扫全量NAS目录
  3. enqueue_qa_batch_notification: 跑完发飞书通知，复用现有notifications队列

依赖(ultralytics/opencv/torch)具体版本还在确认，run_detection_batch内部用局部
import——这个模块本身能被其他代码(比如路由/select_candidate_videos的调用方)
import，不强制要求这几个重依赖已经装好，只有真正跑检测那一刻才需要。
"""
from __future__ import annotations

import json
import random
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification, Video
from app.services.hashing import sha256_bytes_to_hex

SEED = 2025  # 跟学生原脚本一致，不要改——改了就没法复现已经跑过的那批结果

# Raw视频有两个来源根目录，历史遗留导致组织方式不一样(一个按数据集分、一个按人分)，
# 但Processing_Data统一放一起，两条都换到同一个目标根。以后有新Raw来源在这加一条即可。
RAW_TO_PROCESSING_ROOTS = {
    "/mnt/nas/ADSafety/ADSafety/Aerial_Raw_Videos": "/mnt/nas/ADSafety/ADSafety/Aerial_Processing_Data",
    "/mnt/Expansion/data": "/mnt/nas/ADSafety/ADSafety/Aerial_Processing_Data",
}

BATCH_MANIFEST_DIR = Path("/mnt/nas/ADSafety/ADSafety/Aerial_Processing_Data/_qa_batches")


def compute_processing_dir(storage_path: str) -> Path:
    """Raw路径 -> Processing_Data下"这个视频专属"的目录(不含Test，调用方自己拼)。

    规则：匹配到对应Raw根 -> 换成Processing_Data根 -> 中间目录原样保留 ->
    视频文件名(去扩展名)变成新文件夹名。
    """
    p = Path(storage_path)
    for raw_root, processing_root in RAW_TO_PROCESSING_ROOTS.items():
        try:
            rel = p.relative_to(raw_root)
        except ValueError:
            continue
        return Path(processing_root) / rel.parent / rel.stem
    raise ValueError(f"storage_path不在任何已知Raw根目录下，算不出Processing路径: {storage_path}")


async def select_candidate_videos(
    session: AsyncSession, collect_date: date | None = None
) -> list[Video]:
    """挑候选视频：collect_date=None时默认用videos表里最新的collected_at日期。
    只返回Processing目录下还没有Test文件夹的(幂等，已经生成过的不会重复选中)。
    按storage_path排序，保证多次运行时候选顺序稳定，配合固定的随机种子。
    """
    if collect_date is None:
        max_date_result = await session.execute(
            select(func.date_trunc("day", func.max(Video.collected_at)))
        )
        collect_date = max_date_result.scalar_one_or_none()
        if collect_date is None:
            return []

    result = await session.execute(
        select(Video)
        .where(func.date_trunc("day", Video.collected_at) == collect_date)
        .where(Video.deleted_at.is_(None))
        .order_by(Video.storage_path)
    )
    videos = result.scalars().all()

    candidates = []
    for video in videos:
        test_dir = compute_processing_dir(video.storage_path) / "Test"
        if not test_dir.is_dir():
            candidates.append(video)
    return candidates


def _count_boxes(res) -> int:
    """跟学生原脚本一致：优先取obb(旋转框)，没有就取普通boxes。"""
    if res.obb is not None and len(res.obb):
        return len(res.obb)
    if res.boxes is not None and len(res.boxes):
        return len(res.boxes)
    return 0


def run_detection_batch(
    candidates: list[Video],
    *,
    model_path: str,
    model_name: str,
    model_version: str,
    conf: float = 0.5,
) -> dict:
    """对候选视频逐个抽帧+检测，存图到各自Test/目录，返回本批统计。

    命名规则(Test/目录下)：
      raw_f{frame_idx}.jpg                          原始抽帧图
      raw_f{frame_idx}_{model_name}_{model_version}.jpg  检测叠加图
    review结果(通过/不通过)由人工标注环节另外写json，这个函数不管。
    """
    import cv2
    from ultralytics import YOLO

    random.seed(SEED)  # 只调一次，下面按顺序对每个视频调randint，跟原脚本一致
    model = YOLO(model_path)

    manifest_items = []
    results = []
    failed = []

    for video in candidates:
        video_path = Path(video.storage_path)
        test_dir = compute_processing_dir(video.storage_path) / "Test"

        cap = cv2.VideoCapture(str(video_path))
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if n <= 0:
            cap.release()
            failed.append(str(video_path))
            continue

        fidx = random.randint(0, n - 1)
        cap.set(cv2.CAP_PROP_POS_FRAMES, fidx)
        ret, frame = cap.read()
        cap.release()
        if not ret:
            failed.append(str(video_path))
            continue

        res = model(frame, conf=conf, verbose=False)[0]
        n_boxes = _count_boxes(res)

        test_dir.mkdir(parents=True, exist_ok=True)
        raw_name = f"raw_f{fidx}.jpg"
        det_name = f"raw_f{fidx}_{model_name}_{model_version}.jpg"
        cv2.imwrite(str(test_dir / raw_name), frame)
        cv2.imwrite(str(test_dir / det_name), res.plot(labels=False, conf=False))

        sha_hex = sha256_bytes_to_hex(video.sha256)
        results.append({
            "video_sha256": sha_hex,
            "storage_path": str(video_path),
            "test_dir": str(test_dir),
            "frame_idx": fidx,
            "n_boxes": n_boxes,
            "raw_image": str(test_dir / raw_name),
            "detection_image": str(test_dir / det_name),
            "model_name": model_name,
            "model_version": model_version,
        })
        manifest_items.append({"video_sha256": sha_hex, "test_dir": str(test_dir)})

    manifest_path = _write_batch_manifest(manifest_items)
    return {
        "processed": len(results),
        "failed": failed,
        "results": results,
        "manifest_path": str(manifest_path),
    }


def _write_batch_manifest(items: list[dict]) -> Path:
    """批次清单：前端靠读这一个小文件知道"今天这批有哪些"，不用扫全量NAS目录。"""
    BATCH_MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date().isoformat()
    manifest_path = BATCH_MANIFEST_DIR / f"{today}.json"
    manifest_path.write_text(
        json.dumps({"date": today, "items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest_path


async def enqueue_qa_batch_notification(session: AsyncSession, count: int) -> None:
    """复用现有notifications队列，event_type=qa_batch_ready，文案见notifier._format_message。"""
    session.add(
        Notification(
            event_type="qa_batch_ready",
            channel="feishu",
            payload={"count": count},
        )
    )
    await session.commit()
