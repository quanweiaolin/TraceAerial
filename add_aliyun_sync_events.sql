-- ============================================================
-- 阿里云PDS云盘自动下载流程的事件记录表。这块是工程里比较边角料的部分，
-- 故意不展开设计成多张表——一张表用event_type区分不同事件，以后要加新
-- 事件类型(比如"本地视频超期从云端删除")直接加一个新的event_type取值
-- 即可，不用改表结构。
--
-- 重要行为约束(不是表结构层面的，是Python那边触发写入时要遵守)：
-- 只在"真的有事发生"时才INSERT一行，日常定时扫盘发现"什么都没变化"
-- 不写任何记录，这张表不做心跳/巡检日志。目前会写入的场景：
--   event_type='folder_ready'：某个云端文件夹判定上传完成(1小时无新增)，
--     触发下载
--   event_type='issue'：检测到异常，目前已知两种issue_type(自由文本，
--     Python那边固定管理，不建字典表)：
--       reupload_after_complete —— 已判定完成的文件夹又出现新文件
--       unknown_uploader —— 文件夹路径第一级目录名(飞手身份)在operators
--         表里找不到对应的人
--
-- 涉及的视频列表用JSONB数组存(不用单独建关联表)：单次事件涉及的视频最多
-- 几百个，JSONB性能完全够用，而且比关联表更方便人眼直接查内容排查问题。
--
-- 谁上传的：直接编码在PDS文件夹结构里——根目录按人分(每人一个根文件夹+
-- 对应的PDS权限，只能写自己的目录)，不需要单独的pilot_name字段，要用的
-- 时候从cloud_folder_path解析第一级目录名即可。
--
-- videos数组每个元素的字段：
--   cloud_file_id      PDS文件的file_id
--   cloud_path         云端路径，方便人眼核对
--   content_hash       PDS返回的哈希值，云端对不同文件可能用不同算法，
--                       不假设一定是sha256
--   content_hash_algo  "sha256" 或 "sha1"，标明content_hash是用哪种算法算的
--   local_path         下载落地后的本地绝对路径——特意存这个而不是只存
--                       sha256，因为视频瘦身(重新编码)会改变sha256，
--                       单靠sha256回溯会断线，路径比内容哈希更稳定
-- ============================================================

SET search_path TO aerial, public;

CREATE TABLE aliyun_sync_events (
    id                    BIGSERIAL PRIMARY KEY,
    event_type            VARCHAR(32) NOT NULL,   -- 'folder_ready' | 'issue' | 以后再加
    issue_type            VARCHAR(32),            -- 只在event_type='issue'时有意义
    cloud_folder_id       TEXT NOT NULL,          -- PDS文件夹的file_id
    cloud_folder_path     TEXT,                   -- 云端路径，第一级目录名即飞手身份
    videos                JSONB NOT NULL DEFAULT '[]',
    scan_completed_at     TIMESTAMPTZ,            -- 判定"1小时无新增"的那次扫描时间
    download_started_at   TIMESTAMPTZ,
    download_finished_at  TIMESTAMPTZ,
    notified_at           TIMESTAMPTZ,            -- 飞书通知发出的时间
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_aliyun_sync_events_folder ON aliyun_sync_events(cloud_folder_id);
CREATE INDEX idx_aliyun_sync_events_type ON aliyun_sync_events(event_type);

COMMENT ON TABLE aliyun_sync_events IS
    '阿里云PDS自动下载流程的事件记录：文件夹上传完成判定、发现问题(如已判定完成后又有新上传、上传者不在operators表里)等，一条记录对应多个视频(存JSONB数组)；只在真的有事发生时才写入，不记录心跳式巡检';
