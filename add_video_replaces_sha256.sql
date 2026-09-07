-- ============================================================
-- 支持"同一storage_path的视频被重新编码(长期瘦身转码任务)"场景。
-- 现状：转码原地覆盖文件，storage_path不变但内容(进而sha256)变了，
-- file_size_bytes通常会变小。旧sha256记录已经在videos表里躺着了。
--
-- videos.storage_path 上有 idx_videos_storage_path_active 这个部分唯一索引
-- (WHERE deleted_at IS NULL)，意味着同一路径同时只能有一条"活着"的记录。
-- 所以扫盘发现同路径+新sha256时，必须先把旧记录deleted_at标记下线，
-- 再插入新记录，否则会撞上这个唯一索引报错。
--
-- replaces_sha256 记录这条血缘："这条新记录替代了哪个旧sha256"，
-- 不改变videos以sha256去重的核心设计——两份内容完全一样的文件
-- 依然会被认成同一条记录，这条字段只是给"同路径换内容"这一种情况
-- 留一个可追溯的链条。
-- ============================================================

SET search_path TO aerial, public;

ALTER TABLE videos ADD COLUMN replaces_sha256 BYTEA REFERENCES videos(sha256);
COMMENT ON COLUMN videos.replaces_sha256 IS
    '同一storage_path的视频被重新编码时，指向被替代的旧sha256记录(旧记录deleted_at标记下线，不物理删除)';
