-- ============================================================
-- 2026-10-30加：add_video_location_id_column.sql的第二步，删掉已经被
-- location_id取代的videos.site文本列(和它的旧索引idx_videos_city_site)。
--
-- **跑这个文件之前，先确认下面这条SQL结果是0**(location_id已经完全
-- 覆盖了所有原来有site值的行，删site不会丢数据)：
--   SELECT count(*) FROM videos WHERE site IS NOT NULL AND location_id IS NULL;
-- 结果不是0的话，说明add_video_location_id_column.sql的回填没有完全
-- 覆盖(比如这期间有新视频写入了site但还没来得及回填)，先别跑这个文件，
-- 重新跑一遍那条UPDATE语句补齐再来。
--
-- CREATE/DROP INDEX CONCURRENTLY不能跟别的语句包在同一个事务块里，不要
-- 用BEGIN包起来；这里ALTER TABLE DROP COLUMN本身是普通DDL，会短暂加锁，
-- 但videos.site这一列从没被别处依赖(已经确认过全部读写都切到location_id
-- 了)，锁的时间极短，不影响生产。
-- ============================================================

SET search_path TO aerial, public;

DROP INDEX CONCURRENTLY IF EXISTS idx_videos_city_site;
ALTER TABLE videos DROP COLUMN IF EXISTS site;
