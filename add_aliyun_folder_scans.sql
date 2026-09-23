-- ============================================================
-- 阿里云PDS自动下载流程的"心跳"历史表：每次定时扫描(计划6小时一次)，
-- 每个被监控的云端文件夹(MP4/SRT所在的最底层目录)记一行，永久保留，
-- 不做汇总/覆盖——用insert而不是update同一行的JSONB字段，避免Postgres
-- 对越滚越大的JSONB字段做整行重写、越用越慢的问题。
--
-- 瘦身策略：只有这次扫到的大小/文件数跟上一次心跳不一样(changed=true)时，
-- 才把完整文件列表存进files；没变化的心跳(changed=false)只记大小和文件数，
-- 不重复存文件明细，减少体量。
--
-- 不单独维护"当前状态"表——所有需要的"现在状态"都能从这张历史表查出来:
--   - 现在的大小/文件数：这个cloud_folder_id最新一行
--   - 是否连续8次(6h*8=2天)没变：查最近8行size_bytes/file_count是否全一致
--   - 文件级别diff(哪个文件不见了)：找上一条changed=true的记录，
--     拿它的files跟这次的files比
--   - 有没有下载过/在监控删除：查aliyun_sync_events里这个cloud_folder_id
--     有没有folder_ready事件、download_finished_at填了没有
--   "人为删除"检测只在aliyun_sync_events里已经有这个文件夹的folder_ready
--   事件之后才启用，完成前的正常上传波动不算异常。
--
-- files数组每个元素的字段：
--   file_id             PDS文件的file_id
--   name                文件名
--   size_bytes          文件大小
--   content_hash        PDS返回的哈希值(可能是sha1也可能是sha256，不保证)
--   content_hash_algo   "sha256" 或 "sha1"
--   created_at          PDS记录的文件创建时间
-- ============================================================

SET search_path TO aerial, public;

CREATE TABLE aliyun_folder_scans (
    id                 BIGSERIAL PRIMARY KEY,
    drive_id           TEXT NOT NULL,
    cloud_folder_id    TEXT NOT NULL,
    cloud_folder_path  TEXT,
    scanned_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    size_bytes         BIGINT NOT NULL,
    file_count         INTEGER NOT NULL,
    changed            BOOLEAN NOT NULL DEFAULT false,
    files              JSONB  -- 只在changed=true时填充，其余为NULL
);

CREATE INDEX idx_aliyun_folder_scans_folder_time
    ON aliyun_folder_scans(cloud_folder_id, scanned_at DESC);

COMMENT ON TABLE aliyun_folder_scans IS
    '阿里云PDS文件夹心跳历史：每次定时扫描每个被监控文件夹记一行，永久保留，用于判断上传是否完成(连续多次无变化)和检测文件被删除；只在有变化时记录完整文件列表';
