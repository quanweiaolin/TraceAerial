-- ============================================================
-- 2026-11-27加：SRT字段重试计数(srt_retry_count)。
--
-- 背景：视频文件和配套的.srt轨迹文件不一定同时落盘完成——如果扫到视频
-- 那一刻.srt还没传完/还不存在，extract_new_video_fields这次解析不出
-- collect_lon/collect_lat，而scanner.py原来的逻辑只在视频"第一次入库"
-- 那一刻尝试解析SRT一次，之后这条视频已经在videos表里了，每次扫描只会
-- 走"已存在"分支，永远不会再重试——GPS字段就永久卡在NULL，连带
-- location_id也永远分配不上(地址聚类要求有GPS)，2026-11-27发现生产环境
-- 有19条视频是这样卡住的，靠手动脚本backfill_missing_location_id.py
-- 补了一次。
--
-- 这次改成：只要视频的collect_lon还是NULL，"已存在"分支每次扫描都会
-- 重新尝试读一次SRT(文件可能后来才传完)，读到了就顺带补跑地址聚类；
-- 读不到就把这个计数器加1，到10次还是读不到就不再尝试(永久没有SRT的
-- 视频不需要每次扫描都重新stat一次文件，徒增NAS网络IO)。
-- ============================================================

SET search_path TO aerial, public;

ALTER TABLE videos ADD COLUMN IF NOT EXISTS srt_retry_count integer NOT NULL DEFAULT 0;

COMMENT ON COLUMN videos.srt_retry_count IS 'collect_lon仍为NULL时，"已存在视频"分支尝试重新解析SRT失败的次数；达到10次后不再重试。GPS解析成功后这个值不再有意义(不会清零，也不再被读取)';
