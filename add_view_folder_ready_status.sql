-- ============================================================
-- v_folder_ready_status：给标注团队查"哪些会话文件夹现在可以放心开始处理"
-- 用的视图(2026-10-28加，直接给学生用，不用每次来问)。
--
-- 判断逻辑：
--   1. 这个云端文件夹有没有被判定过"下载完成"(aliyun_sync_events里有
--      event_type='folder_ready'且download_finished_at不为空的记录)
--   2. 完成之后有没有发生过"又有新上传"或"文件被删除"(event_type='issue'，
--      issue_type IN ('reupload_after_complete','file_deleted'))
--   3. 如果完成后从来没变动过，或者最后一次变动距今已经超过10天(跟
--      .env里AERIAL_ALIYUN_STABILITY_SCANS_REQUIRED=40、6小时心跳对应的
--      10天稳定期判断逻辑保持一致——如果以后改了这个配置，下面WHERE里
--      '10 days'这个数字要记得跟着改)，才算"稳定完成，可以开始"
--
-- 注意：这是按"会话文件夹"粒度判断的，不是按"地点"(同一个地点可能有好几个
-- 不同日期/设备的会话文件夹)——如果同一个地点还有别的文件夹没完成，这个
-- 视图不会帮你聚合提醒，还是要人工确认同一地点的其他文件夹状态。
-- ============================================================

SET search_path TO aerial, public;

CREATE OR REPLACE VIEW v_folder_ready_status AS
WITH latest_ready AS (
    SELECT DISTINCT ON (cloud_folder_id)
        cloud_folder_id, cloud_folder_path, download_finished_at AS ready_at
    FROM aliyun_sync_events
    WHERE event_type = 'folder_ready' AND download_finished_at IS NOT NULL
    ORDER BY cloud_folder_id, download_finished_at DESC
),
latest_issue AS (
    SELECT cloud_folder_id, max(notified_at) AS last_issue_at
    FROM aliyun_sync_events
    WHERE event_type = 'issue' AND issue_type IN ('reupload_after_complete', 'file_deleted')
    GROUP BY cloud_folder_id
)
SELECT
    r.cloud_folder_path,
    r.ready_at,
    i.last_issue_at,
    (i.last_issue_at IS NULL OR now() - i.last_issue_at > interval '10 days') AS is_stable,
    CASE
        WHEN i.last_issue_at IS NULL THEN '完成后从未变动'
        WHEN now() - i.last_issue_at > interval '10 days' THEN '完成后有变动，已静默超10天，可以开始'
        ELSE '完成后有变动，还在10天观察期内，暂不建议开始'
    END AS status
FROM latest_ready r
LEFT JOIN latest_issue i USING (cloud_folder_id);

-- 学生直接用这个查"现在可以开始的"：
--   SELECT cloud_folder_path, ready_at FROM v_folder_ready_status
--   WHERE is_stable ORDER BY ready_at DESC;
