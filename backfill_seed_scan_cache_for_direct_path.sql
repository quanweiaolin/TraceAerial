-- ============================================================
-- 2026-11-03：AERIAL_SCAN_ROOTS从/mnt/Expansion2-shared换成直连的
-- /mnt/Expansion2之后，scanner.py里"路径+大小一致就复用已算好的哈希"
-- 这个缓存(scan_discoveries表)按完整路径字符串匹配，路径一换缓存全部
-- 失效——导致还没扫到的文件要重新完整读一遍算哈希(5.97TB)，内容其实
-- 根本没变。这条SQL把已知的哈希值"种"进scan_discoveries，让scanner.py
-- 扫到对应的直连路径文件时能直接命中缓存、不用重新读文件。
--
-- 只在“这些文件在扫盘还没扫到之前”跑才有意义——已经被当前这轮扫描
-- 处理过的文件，救不回那部分已经花掉的时间，但能帮后面还没扫到的部分。
-- ============================================================

SET search_path TO aerial, public;

-- 1. dry run，看一下能种多少条
SELECT COUNT(*) FROM videos WHERE storage_path LIKE '/mnt/Expansion2-shared/%' AND deleted_at IS NULL;

-- 2. 建一个合成的scan_job给这批缓存记录挂一个合法的scan_job_id
--    (scan_discoveries.scan_job_id是NOT NULL外键，必须有)
WITH seed_job AS (
    INSERT INTO scan_jobs (root_path, started_at, finished_at, files_found, files_new, files_duplicate, status)
    VALUES ('backfill: expansion2-shared直连路径缓存种子(2026-11-03)', now(), now(), 0, 0, 0, 'completed')
    RETURNING id
)
INSERT INTO scan_discoveries (scan_job_id, file_path, file_size_bytes, hash_status, sha256, discovered_at)
SELECT
    seed_job.id,
    '/mnt/Expansion2/' || substring(storage_path FROM length('/mnt/Expansion2-shared/') + 1),
    file_size_bytes,
    'done',
    sha256,
    now()
FROM videos, seed_job
WHERE storage_path LIKE '/mnt/Expansion2-shared/%' AND deleted_at IS NULL;

-- 3. 验证：种了多少条(应该跟第1步的数字一致)
SELECT COUNT(*) FROM scan_discoveries WHERE file_path LIKE '/mnt/Expansion2/%' AND hash_status = 'done'
  AND discovered_at > now() - interval '5 minutes';
