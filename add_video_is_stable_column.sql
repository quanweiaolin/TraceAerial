-- ============================================================
-- 2026-10-29加：飞行稳定性标记(is_stable)。
--
-- 地址聚类/下游SIFT对齐都假设"拍摄这段视频期间飞机基本停在同一个点"，
-- 单个视频自己的SRT轨迹(逐帧GPS+高度)如果任意两帧之间的总位移超过
-- AERIAL_FLIGHT_STABILITY_MAX_DRIFT_METERS(默认30m)，说明飘了/飞手飞去
-- 了别的地方，这条视频的单点GPS/地址可能不准，标is_stable=False给下游
-- 参考。只有新格式SRT(带rel_alt)才能解析出轨迹，解析不出来(没有SRT/
-- 老格式)永久是NULL(未知)，不是False——没有数据不代表"不稳定"。
--
-- CREATE INDEX CONCURRENTLY不能跟别的语句包在同一个事务块里，不要用
-- BEGIN包起来，不锁表，不影响生产正在跑的扫盘/下载写入。
-- ============================================================

SET search_path TO aerial, public;

ALTER TABLE videos ADD COLUMN IF NOT EXISTS is_stable BOOLEAN;

COMMENT ON COLUMN videos.is_stable IS 'SRT轨迹整体位移是否在AERIAL_FLIGHT_STABILITY_MAX_DRIFT_METERS阈值内；NULL=没有可用SRT轨迹(未知)，不是False';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_videos_is_stable
    ON videos(is_stable) WHERE is_stable = false;
