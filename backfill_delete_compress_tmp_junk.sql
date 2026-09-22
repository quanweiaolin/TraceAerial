-- ============================================================
-- Dry run：找出误扫入 videos 表的视频瘦身(重新编码)转码临时文件。
-- 这类文件名形如 xxx.MP4.compress_tmp.MP4，是瘦身转码过程中的半成品输出，
-- 扫盘时机不巧撞见了它们，源文件已经不在了(转码完成后被清理/重命名掉)。
-- 先跑这个SELECT确认命中的是不是都是预期的这批，确认没问题后再补一个
-- DELETE版本正式清理。
-- ============================================================

SET search_path TO aerial, public;

SELECT COUNT(*)
FROM videos
WHERE original_name LIKE '%.compress_tmp.MP4';

SELECT sha256, original_name, storage_path, file_size_bytes, created_at
FROM videos
WHERE original_name LIKE '%.compress_tmp.MP4'
ORDER BY created_at DESC;

-- ============================================================
-- 确认上面命中的85条都是预期的垫脚石数据后，正式清理。
-- 这些不是真实视频，直接硬删除，不走deleted_at软删除——软删除是给"同一
-- 位置视频被替换成新版本"这种有血缘意义的下线场景用的，这批没有追溯价值。
-- ============================================================

-- 清掉可能指向这些sha256的scan_discoveries关联，避免FK擋住下面的删除
UPDATE scan_discoveries
SET resolved_video_sha256 = NULL
WHERE resolved_video_sha256 IN (
    SELECT sha256 FROM videos WHERE original_name LIKE '%.compress_tmp.MP4'
);

-- video_pipeline_status表如果还没建就跳过这条(线上曾出现过relation不存在)
DELETE FROM video_pipeline_status
WHERE video_sha256 IN (
    SELECT sha256 FROM videos WHERE original_name LIKE '%.compress_tmp.MP4'
);

-- 断开notifications对这批sha256的引用，否则最后DELETE videos会被FK卡住
UPDATE notifications
SET video_sha256 = NULL
WHERE video_sha256 IN (
    SELECT sha256 FROM videos WHERE original_name LIKE '%.compress_tmp.MP4'
);

-- 以防这批垫脚石数据之间互相有replaces_sha256指向，先断开自引用
UPDATE videos
SET replaces_sha256 = NULL
WHERE original_name LIKE '%.compress_tmp.MP4';

DELETE FROM videos
WHERE original_name LIKE '%.compress_tmp.MP4';
