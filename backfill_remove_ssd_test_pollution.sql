-- ============================================================
-- 清理2026-10-07那次SSD暂存测试(aliyun_download_dir临时指向data/)
-- 误扫进正式videos库的数据。当时scan_roots防火墙还没加，下载成功后
-- 自动调用了run_scan，把data/下的测试视频当成正式数据收了进来。
--
-- 范围已确认(见对话记录里的查询结果)：
--   87条videos，storage_path都形如
--     /home/quanwei/Workspace/TraceAerial/data/<年份_城市>_AerialVideos_Q.../...
--   4条scan_jobs(root_path同样指向这批data/路径)
--   devices.device_id IN (1,2) 和 operators.id IN (2,3,4)
--     —— 都是跟着这批测试视频自动建的，已确认没有被其他任何记录引用
-- ============================================================

SET search_path TO aerial, public;

-- 1. dry run，确认范围没变
SELECT COUNT(*) FROM videos WHERE storage_path LIKE '/home/quanwei/Workspace/TraceAerial/data/%AerialVideos%';
SELECT COUNT(*) FROM scan_jobs WHERE root_path LIKE '/home/quanwei/Workspace/TraceAerial/data/%AerialVideos%';

-- 2. 断开notifications对这批视频的引用(没有ON DELETE CASCADE，要先处理)
UPDATE notifications
SET video_sha256 = NULL
WHERE video_sha256 IN (
    SELECT sha256 FROM videos WHERE storage_path LIKE '/home/quanwei/Workspace/TraceAerial/data/%AerialVideos%'
);

-- 3. 删掉这几个测试scan_job(scan_discoveries有ON DELETE CASCADE，会跟着删)
DELETE FROM scan_jobs
WHERE root_path LIKE '/home/quanwei/Workspace/TraceAerial/data/%AerialVideos%';

-- 4. 删掉这87条测试视频(video_pipeline_status有ON DELETE CASCADE，会跟着删)
DELETE FROM videos
WHERE storage_path LIKE '/home/quanwei/Workspace/TraceAerial/data/%AerialVideos%';

-- 5. 删掉只在这次测试里自动建出来的设备/人员记录
DELETE FROM devices WHERE device_id IN (1, 2);
DELETE FROM operators WHERE id IN (2, 3, 4);
