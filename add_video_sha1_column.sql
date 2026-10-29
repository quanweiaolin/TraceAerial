-- ============================================================
-- 给videos表加sha1列，专门给阿里云下载前查重用(2026-10-28讨论：阿里云PDS
-- 返回的content_hash字段本身就是sha1，不是sha256，两种算法不能互相比对，
-- 所以要单独存一份；sha256仍然是内容身份的主键，不受影响)。
--
-- 新列默认NULL：历史视频要跑backfill_video_sha1.py脚本补算(只能补
-- deleted_at IS NULL还活着的记录——已经被瘦身替换、原始字节不在的旧记录，
-- 物理上补不出来，永久是NULL，这是设计上可以接受的)。
--
-- CREATE INDEX CONCURRENTLY不能跟别的语句包在同一个事务块里，这两条分开跑
-- (不要用BEGIN包起来)，不锁表，不影响生产正在跑的扫盘/下载写入。
-- ============================================================

SET search_path TO aerial, public;

ALTER TABLE videos ADD COLUMN IF NOT EXISTS sha1 BYTEA;

-- 部分索引：只索引有值的行，backfill之前大部分是NULL，没必要索引进去
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_videos_sha1
    ON videos(sha1) WHERE sha1 IS NOT NULL;
