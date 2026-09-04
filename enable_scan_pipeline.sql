-- ============================================================
-- 补建"自动扫盘+通知"所需的表，接在 aerial_video_db_schema.sql 之后跑。
-- 当前库里已有 operators/devices/videos，这里只建它们还缺的部分：
--   pipeline_stages(notifications.stage_id外键需要，先建表不用建video_pipeline_status/触发器)
--   scan_jobs / scan_discoveries / notifications
-- 直接照抄 aerial_video_db_schema.sql 里对应的定义，字段/索引保持一致，
-- 额外加了一个 idx_scan_discoveries_lookup 索引(schema.sql原文没有)，
-- 用于run_scan的"按路径+大小查缓存sha256"，没有这个索引这张表随自动扫盘
-- 持续增长后，缓存查找会越来越慢。
-- ============================================================

SET search_path TO aerial, public;

-- pipeline_stages: notifications.stage_id 外键依赖，只建表结构，不插种子数据——
-- 具体有哪些阶段、每个阶段怎么描述还在讨论中(比如inference阶段的设计)，
-- 暂时留空表不影响notifications工作(scanner.py发通知从来不传stage_id，这列可空)。
-- 等阶段定下来了再INSERT，不用改表结构。
-- 同理暂不建 video_pipeline_status 和触发器(那部分等真正要上标注/推理流程再说)
CREATE TABLE pipeline_stages (
    id            SERIAL PRIMARY KEY,
    code          VARCHAR(32) UNIQUE NOT NULL,
    name_cn       VARCHAR(64) NOT NULL,
    stage_order   INT NOT NULL,
    is_active     BOOLEAN NOT NULL DEFAULT true,
    description   TEXT
);

-- 扫盘任务与发现记录
CREATE TABLE scan_jobs (
    id             BIGSERIAL PRIMARY KEY,
    root_path      TEXT NOT NULL,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ,
    files_found    INT DEFAULT 0,
    files_new      INT DEFAULT 0,
    files_duplicate INT DEFAULT 0,
    status         VARCHAR(16) NOT NULL DEFAULT 'running',
    error_message  TEXT
);
COMMENT ON TABLE scan_jobs IS '每次扫盘的批次记录，Python扫盘服务负责写入';

CREATE TABLE scan_discoveries (
    id                    BIGSERIAL PRIMARY KEY,
    scan_job_id           BIGINT NOT NULL REFERENCES scan_jobs(id) ON DELETE CASCADE,
    file_path             TEXT NOT NULL,
    file_size_bytes       BIGINT,
    hash_status           VARCHAR(16) NOT NULL DEFAULT 'pending',
    sha256                BYTEA,
    resolved_video_sha256 BYTEA REFERENCES videos(sha256),
    is_new_task           BOOLEAN,
    error_message         TEXT,
    discovered_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_scan_discoveries_job ON scan_discoveries(scan_job_id);
CREATE INDEX idx_scan_discoveries_hash_status ON scan_discoveries(hash_status);
-- 缓存查找用：run_scan算hash前先按(file_path, file_size_bytes)查有没有算过，
-- 命中且hash_status='done'就直接复用，不用重新读文件
CREATE INDEX idx_scan_discoveries_lookup ON scan_discoveries(file_path, file_size_bytes);
COMMENT ON TABLE scan_discoveries IS '扫盘发现的每个文件一行；sha256算完后决定是插入videos新任务还是仅关联已有记录';

-- 通知（飞书等），状态机由Python后台任务驱动
CREATE TABLE notifications (
    id                 BIGSERIAL PRIMARY KEY,
    video_sha256        BYTEA REFERENCES videos(sha256),
    stage_id            INT REFERENCES pipeline_stages(id),
    event_type          VARCHAR(32) NOT NULL,
    target_operator_id  INT REFERENCES operators(id),
    channel             VARCHAR(16) NOT NULL DEFAULT 'feishu',
    payload             JSONB NOT NULL DEFAULT '{}',
    status               VARCHAR(16) NOT NULL DEFAULT 'pending',
    retry_count          INT NOT NULL DEFAULT 0,
    sent_at              TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_notifications_status ON notifications(status);
COMMENT ON TABLE notifications IS '通知发送队列：应用层插pending行，后台worker轮询发送飞书消息并回写status/sent_at';
