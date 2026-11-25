"""
地址人工复核——视觉校验：比较"候选地址的参考截图"和"视频抽帧"是否拍的是
同一个地方。

自己实现SIFT+RANSAC特征匹配，不import extern/AD_pipeline_1的
point_matching.py——那个仓库是独立的第三方流水线，这里只是借鉴它的
算法思路(SIFT关键点+knn比值筛选+RANSAC单应矩阵求内点)，不跨仓库依赖。

跟video_metadata.py里probe_video/extract_gps_from_srt同一个原则："能自动
判断就判断，判断不出来就返回描述性的失败结果，不向上抛异常中断主流程"——
compare_frames对任何中间步骤失败(读图失败/特征点不够/单应矩阵求不出来)
都返回matched=False而不是raise，因为视觉校验只是人工复核流程里的辅助
参考，不能让它的内部异常打断整个复核请求。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import cv2
import numpy as np

# RANSAC求单应矩阵的重投影误差阈值(像素)：误差超过这个值的匹配点对不计入内点。
_RANSAC_REPROJ_THRESHOLD = 4.0
_RANSAC_MAX_ITERS = 3000
_RANSAC_CONFIDENCE = 0.995

# knn匹配比值测试阈值：最近邻距离/次近邻距离 < 0.8 才算"好的匹配"，
# 过滤掉模糊/重复纹理场景里容易产生的误匹配。
_KNN_RATIO_THRESHOLD = 0.8

# 自动判定为"同一地点"的门槛，跟match_policy.with_auto_acceptance里用的数值
# 一致(内点数≥12 且 内点率≥0.35)，逻辑在这里单独写一遍，不import那个文件。
_MIN_INLIERS = 12
_MIN_INLIER_RATIO = 0.35

_NO_HOMOGRAPHY_RESULT = {
    "matched": False,
    "inliers": 0,
    "inlier_ratio": 0.0,
    "auto_accepted": False,
    "auto_reason": "无法估计有效对齐矩阵",
}


def extract_frame(video_path: str, output_path: str) -> None:
    """用ffmpeg从视频里截第一帧存成图片，跟video_metadata.py里
    probe_video调ffprobe走的是同一个subprocess惯例(capture_output+text+
    timeout，非0退出码时把stderr塞进异常信息里，方便排查)。

    这里不用check=True直接让CalledProcessError冒泡，是因为想在异常信息
    里带上ffmpeg自己打印的stderr原因(比如"文件不是视频"/"编码器不支持")，
    而不是只有一个退出码——跟调用方(视觉校验流程)约定好统一抛ValueError，
    不关心具体是subprocess哪一层出的错。
    """
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(video_path),
                "-vframes", "1", "-q:v", "2", str(output_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise ValueError(f"截图提取失败: {exc}") from exc

    if proc.returncode != 0:
        raise ValueError(f"截图提取失败: {proc.stderr}")

    if not Path(output_path).is_file():
        raise ValueError(f"截图提取失败: {proc.stderr}")


def _auto_accept(inliers: int, inlier_ratio: float) -> bool:
    """跟match_policy.with_auto_acceptance的门槛数值一致(内点数≥12 且
    内点率≥0.35)，逻辑自己写一个小函数，不import那个文件。"""
    return inliers >= _MIN_INLIERS and inlier_ratio >= _MIN_INLIER_RATIO


def compare_frames(frame_a_path: str, frame_b_path: str) -> dict:
    """拿两张图(候选地址参考截图 vs 视频抽帧)做SIFT+RANSAC视觉比对。

    思路借鉴(不import) extern/AD_pipeline_1里point_matching.py的
    _image_homography：灰度化→SIFT关键点+描述子→BFMatcher(NORM_L2)的
    knnMatch(k=2)+0.8倍比值测试筛好的匹配→少于4对匹配直接判"无法估计"
    (findHomography至少需要4对点)→RANSAC求单应矩阵H和内点mask→H求不出来
    (None)也判"无法估计"→inliers=mask里1的个数，inlier_ratio=inliers/
    好的匹配总数→按内点数≥12且内点率≥0.35判auto_accepted。

    任何一步失败(读图失败/找不到足够特征点/单应矩阵求不出来)都返回
    描述性的失败结果，不向上抛异常——这只是人工复核流程的辅助参考，不能
    让内部异常打断整个复核请求。
    """
    img_a = cv2.imread(frame_a_path, cv2.IMREAD_GRAYSCALE)
    img_b = cv2.imread(frame_b_path, cv2.IMREAD_GRAYSCALE)
    if img_a is None or img_b is None:
        return dict(_NO_HOMOGRAPHY_RESULT)

    sift = cv2.SIFT_create()
    kp_a, desc_a = sift.detectAndCompute(img_a, None)
    kp_b, desc_b = sift.detectAndCompute(img_b, None)
    if desc_a is None or desc_b is None or len(kp_a) < 2 or len(kp_b) < 2:
        return dict(_NO_HOMOGRAPHY_RESULT)

    matcher = cv2.BFMatcher(cv2.NORM_L2)
    try:
        knn_matches = matcher.knnMatch(desc_b, desc_a, k=2)
    except cv2.error:
        return dict(_NO_HOMOGRAPHY_RESULT)

    good_matches = [
        pair[0]
        for pair in knn_matches
        if len(pair) == 2 and pair[0].distance < _KNN_RATIO_THRESHOLD * pair[1].distance
    ]
    if len(good_matches) < 4:
        return dict(_NO_HOMOGRAPHY_RESULT)

    src_pts = np.float32([kp_b[m.queryIdx].pt for m in good_matches]).reshape(-1, 1, 2)
    dst_pts = np.float32([kp_a[m.trainIdx].pt for m in good_matches]).reshape(-1, 1, 2)

    try:
        homography, mask = cv2.findHomography(
            src_pts, dst_pts, cv2.RANSAC,
            _RANSAC_REPROJ_THRESHOLD,
            maxIters=_RANSAC_MAX_ITERS,
            confidence=_RANSAC_CONFIDENCE,
        )
    except cv2.error:
        return dict(_NO_HOMOGRAPHY_RESULT)

    if homography is None or mask is None:
        return dict(_NO_HOMOGRAPHY_RESULT)

    inliers = int(mask.sum())
    inlier_ratio = inliers / len(good_matches)
    auto_accepted = _auto_accept(inliers, inlier_ratio)

    return {
        "matched": True,
        "inliers": inliers,
        "inlier_ratio": inlier_ratio,
        "auto_accepted": auto_accepted,
        "auto_reason": "内点数与内点率均达标" if auto_accepted else "内点数或内点率未达标，需人工复核",
    }
