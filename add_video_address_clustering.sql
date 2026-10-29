-- ============================================================
-- 2026-10-29加：地址聚类(GPS+高度)，给下游多视频SIFT对齐/稳像用。
--
-- videos表加4列：
--   city          采集地点城市，直接从storage_path里符合会话命名规范
--                 (session_naming.SESSION_FOLDER_RE)那一段读出来
--   site          聚类后的最终地址标签(如panshiroad_1)，不是文件夹名里
--                 裸的site分段值——同一个物理地点可能被多次飞行，每次
--                 文件夹名字段写的site一样，但GPS聚类后才知道具体是
--                 "第几个"地址。一旦分配给某个视频就不可再改
--   flight_height 文件夹名里的标称飞行高度(米)，纯文本解析，跟GPS/聚类
--                 无关
--   srt_height    SRT遥测字幕里实际记录的高度(rel_alt，相对起飞点)，跟
--                 flight_height分开存；只有新格式DJI机型能解析出来，
--                 老格式永久是NULL(见services/video_metadata.py注释)
--
-- 只有storage_path符合会话命名规范(当前约5.2%)的视频才会有值，剩下的
-- 94.8%老命名方式的视频这4列永久留空，不做特殊处理——没有这个前提就
-- 没有city/site/date这些字段可用，没法聚类。
--
-- 新建locations表：地址注册表，只存地址标签+聚类时生效的阈值+创建时间，
-- 不存中心点坐标——领导者算法判断新视频能不能归到某个已有地址时，要跟
-- "簇内全部已有成员"比距离，而这些成员坐标本来就在videos表里(filter
-- city+site就是该地址全部成员)，不用在这里重复存一份，也不会两份数据
-- 不同步。threshold_meters记录"这个地址是在哪个阈值下诞生的"，供以后
-- .env里的阈值改了之后回头追溯用，不影响已经分配好的videos.site。
--
-- CREATE INDEX CONCURRENTLY不能跟别的语句包在同一个事务块里，不要用
-- BEGIN包起来，不锁表，不影响生产正在跑的扫盘/下载写入。
-- ============================================================

SET search_path TO aerial, public;

ALTER TABLE videos ADD COLUMN IF NOT EXISTS city TEXT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS site TEXT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS flight_height INTEGER;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS srt_height DOUBLE PRECISION;

COMMENT ON COLUMN videos.city IS '采集地点城市，从storage_path里符合会话命名规范的那段读出，老命名方式的历史视频永久为NULL';
COMMENT ON COLUMN videos.site IS 'GPS聚类后的最终地址标签(如panshiroad_1)，一旦分配不可再改；没有GPS坐标的视频永久为NULL';
COMMENT ON COLUMN videos.flight_height IS '文件夹名里的标称飞行高度(米)，纯文本解析，跟srt_height(SRT实测高度)分开存';
COMMENT ON COLUMN videos.srt_height IS 'SRT遥测记录的实际高度(rel_alt，相对起飞点)，只有新格式DJI机型能解析出来，老格式永久为NULL';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_videos_city_site
    ON videos(city, site) WHERE site IS NOT NULL;

CREATE TABLE IF NOT EXISTS locations (
    id               BIGSERIAL PRIMARY KEY,
    city             TEXT NOT NULL,
    site             TEXT NOT NULL,
    threshold_meters DOUBLE PRECISION NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (city, site)
);

COMMENT ON TABLE locations IS 'GPS聚类得到的地址注册表：只记录地址标签+聚类时生效的阈值+创建时间，不存中心点坐标，簇内全部成员坐标直接查videos表(city+site)即可';
COMMENT ON COLUMN locations.threshold_meters IS '这个地址诞生时AERIAL_ADDRESS_CLUSTER_MAX_DIAMETER_METERS的生效值，供以后阈值改了之后回头追溯用，不影响已分配的videos.site';
