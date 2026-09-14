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

from app.config import settings
from app.models import Notification, Video
from app.services.hashing import sha256_bytes_to_hex

SEED = 2025  # 跟学生原脚本一致，不要改——改了就没法复现已经跑过的那批结果

# 目标根目录从配置读(AERIAL_VRU_PROCESSING_ROOT)，NAS权限没弄好之前可以先指向
# 本地目录测试功能，权限好了改.env、重启服务即可切回NAS，不用改代码/重新部署。
PROCESSING_ROOT = settings.vru_processing_root

# Raw视频有两个来源根目录，历史遗留导致组织方式不一样(一个按数据集分、一个按人分)，
# 但Processing_Data统一放一起，两条都换到同一个目标根。以后有新Raw来源在这加一条即可。
RAW_TO_PROCESSING_ROOTS = {
    "/mnt/nas/ADSafety/ADSafety/Aerial_Raw_Videos": PROCESSING_ROOT,
    "/mnt/Expansion/data": PROCESSING_ROOT,
}

BATCH_MANIFEST_DIR = Path(PROCESSING_ROOT) / "_qa_batches"


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
    session: AsyncSession,
    collect_date: date | None = None,
    only_latest_date: bool = True,
    limit: int | None = None,
) -> list[Video]:
    """挑候选视频，只返回Processing目录下还没有Test文件夹的(幂等，已经生成过的
    不会重复选中)。按storage_path排序，保证多次运行时候选顺序稳定，配合固定的
    随机种子。

    三种用法：
      - collect_date给了具体日期 -> 只挑那一天
      - collect_date=None, only_latest_date=True(默认) -> 挑videos表里最新的
        collected_at那一天，给定时任务用("有新数据了")
      - collect_date=None, only_latest_date=False -> 不限日期，挑所有还没有
        Test文件夹的视频，给手动"清洗已有数据"用

    limit: 只取按storage_path排序后的前N个(在"是否已有Test文件夹"过滤之前取)，
    主要给手动小范围测试用，比如"只测某一天的前10个视频"。
    """
    stmt = select(Video).where(Video.deleted_at.is_(None))

    if collect_date is not None:
        stmt = stmt.where(func.date_trunc("day", Video.collected_at) == collect_date)
    elif only_latest_date:
        max_date_result = await session.execute(
            select(func.date_trunc("day", func.max(Video.collected_at)))
        )
        latest_date = max_date_result.scalar_one_or_none()
        if latest_date is None:
            return []
        stmt = stmt.where(func.date_trunc("day", Video.collected_at) == latest_date)
    # else: only_latest_date=False 且没给collect_date -> 不加日期条件，全表候选

    stmt = stmt.order_by(Video.storage_path)
    if limit is not None:
        stmt = stmt.limit(limit)

    result = await session.execute(stmt)
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


# 按类别固定配色(BGR，cv2通道顺序)，不用ultralytics res.plot()自带的按类别
# 自动配色——它是按调色板顺序轮着分配的，容易分到偏浅/偏白、看不清的颜色。
# 这里手动挑8个互相区分度高、对着沥青路面/天空/绿化都清楚的颜色；跟模型
# best_vru_universal.pt的类别对应(之前调试时打印过model.names确认过)。
# 以后模型换了/加了新类别，在这个字典里加一条就行，不用改画框逻辑。
CLASS_COLORS = {
    "car": (0, 255, 255),               # 黄
    "truck": (0, 140, 255),             # 橙
    "bus": (0, 0, 255),                 # 红
    "motorcycle": (255, 0, 255),        # 品红
    "pedestrian": (0, 255, 0),          # 绿(VRU里最需要重点关注)
    "bicycle": (255, 255, 0),           # 青
    "tricycle": (255, 0, 0),            # 蓝
    "engineering_vehicle": (204, 0, 102),  # 紫
}
DEFAULT_BOX_COLOR = (147, 20, 255)  # 没在上面字典里配到的类别用这个兜底(深粉)，不用白色
DETECTION_BOX_THICKNESS = 3


def _color_for_class(res, cls_id: int) -> tuple[int, int, int]:
    class_name = res.names.get(cls_id, "") if hasattr(res, "names") else ""
    return CLASS_COLORS.get(class_name, DEFAULT_BOX_COLOR)


def _draw_detections(frame, res):
    """手动画检测框，按类别固定配色，代替res.plot()的自动配色。
    跟_count_boxes一样区分obb(旋转框，4个顶点)和普通boxes(左上右下两点)。
    """
    import cv2  # 局部import，理由见模块顶部docstring(重依赖不强制全局装好)

    annotated = frame.copy()
    if res.obb is not None and len(res.obb):
        points = res.obb.xyxyxyxy.cpu().numpy().astype(int)  # (N, 4, 2)
        classes = res.obb.cls.cpu().numpy().astype(int)
        for pts, cls_id in zip(points, classes):
            cv2.polylines(
                annotated, [pts.reshape(-1, 1, 2)], isClosed=True,
                color=_color_for_class(res, cls_id), thickness=DETECTION_BOX_THICKNESS,
            )
    elif res.boxes is not None and len(res.boxes):
        boxes = res.boxes.xyxy.cpu().numpy().astype(int)
        classes = res.boxes.cls.cpu().numpy().astype(int)
        for (x1, y1, x2, y2), cls_id in zip(boxes, classes):
            cv2.rectangle(
                annotated, (int(x1), int(y1)), (int(x2), int(y2)),
                _color_for_class(res, cls_id), DETECTION_BOX_THICKNESS,
            )
    return annotated


def run_detection_batch(
    candidates: list[Video],
    *,
    model_path: str,
    model_name: str,
    model_version: str,
    conf: float = 0.4,
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
        cv2.imwrite(str(test_dir / det_name), _draw_detections(frame, res))

        sha_hex = sha256_bytes_to_hex(video.sha256)
        item = {
            "video_sha256": sha_hex,
            "storage_path": str(video_path),
            "test_dir": str(test_dir),
            "frame_idx": fidx,
            "n_boxes": n_boxes,
            "raw_image": str(test_dir / raw_name),
            "detection_image": str(test_dir / det_name),
            "model_name": model_name,
            "model_version": model_version,
        }
        results.append(item)
        manifest_items.append(item)  # 清单存完整信息，前端靠这个就能展示，不用猜文件名

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


def list_batch_dates() -> list[str]:
    """列出有哪些批次(按日期)，供前端选择要审核哪一批。"""
    if not BATCH_MANIFEST_DIR.is_dir():
        return []
    return sorted((p.stem for p in BATCH_MANIFEST_DIR.glob("*.json")), reverse=True)


def read_batch_manifest(date_str: str) -> dict | None:
    """读某一天的批次清单，返回run_detection_batch当时写进去的完整条目列表。"""
    path = BATCH_MANIFEST_DIR / f"{date_str}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def review_json_path(test_dir: str, frame_idx: int, model_name: str, model_version: str) -> Path:
    """review结果json路径，跟检测叠加图同目录、同名规则，方便肉眼对应。"""
    return Path(test_dir) / f"raw_f{frame_idx}_{model_name}_{model_version}_review.json"


def read_review(test_dir: str, frame_idx: int, model_name: str, model_version: str) -> dict | None:
    """读一条review结果，没标注过返回None。"""
    path = review_json_path(test_dir, frame_idx, model_name, model_version)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_review(
    test_dir: str,
    frame_idx: int,
    model_name: str,
    model_version: str,
    verdict: str,
    reviewed_by: str | None = None,
    note: str = "",
) -> None:
    """写一条review结果。verdict目前用"pass"/"fail"两种取值。"""
    path = review_json_path(test_dir, frame_idx, model_name, model_version)
    data = {
        "verdict": verdict,
        "reviewed_by": reviewed_by,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "note": note,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


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
