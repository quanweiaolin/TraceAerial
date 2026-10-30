-- ============================================================
-- 2026-10-30加：videos表的地址字段改成外键(location_id)，不再直接存
-- site文本。
--
-- 之前的设计(见add_video_address_clustering.sql)是videos.site直接存
-- 地址标签文本(如panshiroad_1)，跟locations.site重复存了一份。问题：
-- 如果以后要给一个地址改名(比如起名太绕口想换个更合适的，这个场景在
-- 2026-10-30讨论里真实提到过)，就要把这个地址下所有videos行的site都
-- 批量UPDATE一遍——改成外键之后，只改locations表那一行就够了，跟
-- devices/operators那套"videos存id，文本只在字典表存一份"的风格一致。
--
-- 这个迁移分两步(这个文件只做第一步)：
--   1. (这个文件)加location_id列，从现有的(city, site)文本匹配回填，
--      不删旧的site列——先跑完这个，自己查一下下面这条验证SQL结果是不是0，
--      确认回填完全覆盖了，再去跑第二个文件
--        SELECT count(*) FROM videos WHERE site IS NOT NULL AND location_id IS NULL;
--   2. (另一个文件drop_video_site_column.sql)确认没问题后，再删掉
--      videos.site这一列和它的旧索引
--
-- CREATE INDEX CONCURRENTLY不能跟别的语句包在同一个事务块里，不要用
-- BEGIN包起来，不锁表，不影响生产正在跑的扫盘/下载写入。
-- ============================================================

SET search_path TO aerial, public;

ALTER TABLE videos ADD COLUMN IF NOT EXISTS location_id BIGINT REFERENCES locations(id);

-- 从已有的(city, site)文本匹配回填——locations表的UNIQUE(city, site)
-- 保证这个匹配是唯一确定的，不会出现一对多
UPDATE videos v
SET location_id = l.id
FROM locations l
WHERE v.city = l.city AND v.site = l.site AND v.location_id IS NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_videos_location_id
    ON videos(location_id) WHERE location_id IS NOT NULL;

COMMENT ON COLUMN videos.location_id IS '指向locations表的外键，取代原来直接存文本的site列；一旦分配不可再改，没有GPS坐标/飞行不稳定/不符合命名规范的视频永久为NULL';
