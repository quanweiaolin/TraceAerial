-- ============================================================
-- 航测视频数据管理系统 - PostgreSQL 数据库设计
-- 版本: v1.0
-- 适用: PostgreSQL 14+
-- 设计原则:
--   1. videos 主表用 sha256 做全局唯一主键
--   2. 流水线进度用"可配置阶段表 + 状态表"实现，加新阶段无需改表结构
--   3. 各阶段产出(标注/推理/追踪等)数据量大，一律只存"存储地址+元信息"，
--      不把大 JSON/二进制本体放进数据库
--   4. 所有分类型字段(场景层级、事件类型)用查找表而非 ENUM，方便运营侧自助扩展
-- ============================================================

CREATE SCHEMA IF NOT EXISTS aerial;
SET search_path TO aerial, public;

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- 生成 UUID / 校验用
-- 如需按 GPS 坐标做空间检索，取消下面注释(需数据库已安装 postgis)
-- CREATE EXTENSION IF NOT EXISTS postgis;
-- 全文未使用，后续可以添加uuid，sha265等
-- ============================================================
-- 0. 基础字典表：操作人 / 设备
-- ============================================================

CREATE TABLE operators (
    id            SERIAL PRIMARY KEY,
    username      VARCHAR(64) UNIQUE NOT NULL,
    display_name  VARCHAR(64),
    role          VARCHAR(32),           -- 核心学生，管理员，标注员，飞手
    dept          VARCHAR(64),
    joined_at           DATE,            -- 入职/加入项目时间
    left_at             DATE,            -- 离职时间；仍在职则为NULL
    employment_status   VARCHAR(16) NOT NULL DEFAULT 'active'
        CHECK (employment_status IN ('active', 'inactive', 'left')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON COLUMN operators.employment_status IS 'active=在职, inactive=暂停(如休假), left=已离职；通知服务据此过滤，避免给离职人员发送提醒';

CREATE TABLE devices (
    device_id     SERIAL PRIMARY KEY,
    device_code   VARCHAR(64) UNIQUE NOT NULL,
    model         VARCHAR(64),           -- 如 Pegasus TRK/HD1000 等型号
    sensor_config JSONB DEFAULT '{}',    -- 传感器/相机层配置，自由扩展
    owner_unit    VARCHAR(128),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- 1. 核心主表：视频
-- ============================================================

CREATE TABLE videos (
    sha256            BYTEA PRIMARY KEY,             -- sha256(视频文件)，32字节
    original_name     TEXT NOT NULL,
    storage_path      TEXT NOT NULL,                 -- 原始视频存储地址(OSS/NFS/S3等)
    file_size_bytes    BIGINT,
    duration_seconds   NUMERIC(10,3),
    width             INT,
    height            INT,
    fps               NUMERIC(6,3),
    codec             VARCHAR(32),

    device_id         INT REFERENCES devices(device_id),
    collected_at      TIMESTAMPTZ,                    -- 采集时间
    collected_by      INT REFERENCES operators(id),
    collect_lon       NUMERIC(10,6),                  -- 采集经度(不装postgis时的降级方案)
    collect_lat       NUMERIC(10,6),                  -- 采集纬度
    -- collect_geom   GEOGRAPHY(POINT,4326),          -- 若启用postgis可用此列替代经纬度

    -- 2026-10-29加(见add_video_address_clustering.sql)：地址聚类，给下游
    -- 多视频SIFT对齐/稳像用。只有storage_path符合会话命名规范的视频才有值
    city              TEXT,                           -- 采集地点城市，从storage_path解析
    site              TEXT,                           -- GPS聚类后的最终地址标签(如panshiroad_1)，一旦分配不可再改
    flight_height     INTEGER,                        -- 文件夹名里的标称飞行高度(米)
    srt_height        DOUBLE PRECISION,                -- SRT遥测实际高度(rel_alt)，老格式SRT永久为NULL

    -- 2026-10-29加(见add_video_is_stable_column.sql)：SRT轨迹整体位移是否
    -- 在阈值内；NULL=没有可用SRT轨迹(未知，不是False)
    is_stable         BOOLEAN,

    remark            TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at        TIMESTAMPTZ                        -- 软删除；非空表示已下线/作废
);
COMMENT ON TABLE videos IS '视频主表，sha256为全局唯一主键';
COMMENT ON COLUMN videos.sha256 IS '视频文件内容的SHA256哈希，作为主键防止重复入库';
COMMENT ON COLUMN videos.storage_path IS '原始文件存储地址；同一路径若被新文件覆盖会产生新的sha256记录，旧记录靠deleted_at标记下线，不做物理删除';
COMMENT ON COLUMN videos.deleted_at IS '软删除时间戳；为空=有效记录。删除走UPDATE而非DELETE，避免级联清空标注/推理等明细表';

-- 同一存储路径只允许一条"未删除"的有效记录，防止重复扫描入库；
-- 文件被覆盖时：先UPDATE旧记录SET deleted_at=now()，再INSERT新sha256记录
CREATE UNIQUE INDEX idx_videos_storage_path_active
    ON videos(storage_path) WHERE deleted_at IS NULL;

CREATE INDEX idx_videos_collected_at ON videos(collected_at);
CREATE INDEX idx_videos_device ON videos(device_id);
CREATE INDEX idx_videos_city_site ON videos(city, site) WHERE site IS NOT NULL;
CREATE INDEX idx_videos_is_stable ON videos(is_stable) WHERE is_stable = false;

-- GPS聚类得到的地址注册表：只存地址标签+聚类时生效的阈值+创建时间，不存
-- 中心点坐标——簇内全部成员坐标直接查videos表(city+site过滤)即可，不用
-- 重复存一份。见add_video_address_clustering.sql
CREATE TABLE locations (
    id               BIGSERIAL PRIMARY KEY,
    city             TEXT NOT NULL,
    site             TEXT NOT NULL,
    threshold_meters DOUBLE PRECISION NOT NULL,       -- 这个地址诞生时生效的最大簇内距离(米)
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (city, site)
);
COMMENT ON TABLE locations IS 'GPS聚类得到的地址注册表，不存中心点坐标，簇内全部成员坐标直接查videos表(city+site)';

-- ============================================================
-- 2. 流水线进度跟踪（核心：可扩展 + 展示每个视频的进展）
-- ============================================================

-- 阶段定义表：加新阶段只需 INSERT 一行，无需改任何表结构
CREATE TABLE pipeline_stages (
    id            SERIAL PRIMARY KEY,
    code          VARCHAR(32) UNIQUE NOT NULL,   -- 程序里用的标识符
    name_cn       VARCHAR(64) NOT NULL,
    stage_order   INT NOT NULL,                  -- 展示/排序用
    is_active     BOOLEAN NOT NULL DEFAULT true,
    description   TEXT
);

INSERT INTO pipeline_stages (code, name_cn, stage_order, description) VALUES
    ('collection',          '采集',     10, '视频采集入库，记录来源地址与设备信息'),
    ('scene_tagging',       '场景填写', 20, '按Pegasus场景层级填写场景标签'),
    ('annotation',          '标注',     30, '标注数据生成，JSON存对象存储，仅记录地址'),
    ('inference',           '推理',     40, '模型推理产出检测/分类结果'),
    ('tracking',            '追踪',     50, '基于推理结果做多目标追踪'),
    ('dataset_generation',  '生成数据', 60, '汇总各阶段产出，生成训练/发布数据集'),
    ('scene_extraction',    '场景提取', 70, '提取cut-in/cut-out等场景事件'),
    ('visualization',       '可视化',   80, '生成可视化预览/看板所需数据');

CREATE TYPE stage_status AS ENUM (
    'pending', 'in_progress', 'completed', 'failed', 'skipped', 'blocked'
);

-- 状态表：每个视频 x 每个阶段一行，是"进展展示"的直接数据源
CREATE TABLE video_pipeline_status (
    id                BIGSERIAL PRIMARY KEY,
    video_sha256      BYTEA NOT NULL REFERENCES videos(sha256) ON DELETE CASCADE,
    stage_id          INT NOT NULL REFERENCES pipeline_stages(id),
    status            stage_status NOT NULL DEFAULT 'pending',
    progress_percent  SMALLINT NOT NULL DEFAULT 0 CHECK (progress_percent BETWEEN 0 AND 100),
    operator_id       INT REFERENCES operators(id),
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ,
    error_message     TEXT,
    ref_table         VARCHAR(64),     -- 指向该阶段明细表名，如 'annotation_batches'
    ref_id            BIGINT,          -- 指向明细表的主键，方便前端一跳直达详情
    metadata          JSONB NOT NULL DEFAULT '{}',
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (video_sha256, stage_id)
);

COMMENT ON TABLE video_pipeline_status IS '进度总表：每个视频x每个阶段一行，是前端进展看板的直接数据源';
COMMENT ON COLUMN video_pipeline_status.ref_table IS '该阶段产出数据所在的明细表名，如annotation_batches，供前端一跳直达详情页';
COMMENT ON COLUMN video_pipeline_status.ref_id IS '配合ref_table定位到明细表具体的哪一行';

CREATE INDEX idx_vps_status ON video_pipeline_status(status);
CREATE INDEX idx_vps_stage  ON video_pipeline_status(stage_id);
CREATE INDEX idx_vps_video  ON video_pipeline_status(video_sha256);

-- 新视频入库时，自动为它初始化所有阶段的状态行(均为pending)
CREATE OR REPLACE FUNCTION fn_init_pipeline_status() RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO video_pipeline_status (video_sha256, stage_id)
    SELECT NEW.sha256, id FROM pipeline_stages WHERE is_active = true;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_init_pipeline_status
AFTER INSERT ON videos
FOR EACH ROW EXECUTE FUNCTION fn_init_pipeline_status();

-- 看板视图：一个视频一行，各阶段状态汇总成JSON，前端/可视化直接查这个视图
CREATE VIEW v_video_progress AS
SELECT
    v.sha256,
    v.original_name,
    v.collected_at,
    jsonb_object_agg(
        ps.code,
        jsonb_build_object(
            'name', ps.name_cn,
            'status', vps.status,
            'progress', vps.progress_percent,
            'updated_at', vps.updated_at
        ) ORDER BY ps.stage_order
    ) AS stages
FROM videos v
JOIN video_pipeline_status vps ON vps.video_sha256 = v.sha256
JOIN pipeline_stages ps ON ps.id = vps.stage_id
WHERE v.deleted_at IS NULL
GROUP BY v.sha256, v.original_name, v.collected_at;

-- 数据量大(10K+视频)时，上面这个普通视图每次查询都要重新聚合，建议改用物化视图定期刷新：
-- CREATE MATERIALIZED VIEW mv_video_progress AS SELECT * FROM v_video_progress;
-- CREATE UNIQUE INDEX ON mv_video_progress(sha256);
-- 定时任务里执行: REFRESH MATERIALIZED VIEW CONCURRENTLY mv_video_progress;
-- (CONCURRENTLY 需要上面这个唯一索引才能用，否则会锁表)

-- ============================================================
-- 3. 场景填写（Pegasus 场景层级）
-- ============================================================

-- 场景分类树：支持多层级(level字段对应Pegasus第几层)，parent_id实现树形结构
CREATE TABLE scene_taxonomy (
    id          SERIAL PRIMARY KEY,
    parent_id   INT REFERENCES scene_taxonomy(id),
    level       INT NOT NULL,               -- 第几层
    code        VARCHAR(64) UNIQUE NOT NULL,
    name_cn     VARCHAR(128) NOT NULL,
    description TEXT
);

COMMENT ON COLUMN scene_taxonomy.level IS 'Pegasus场景层级，1=一级场景大类，2=二级细分，可继续往下加';

-- Seed: Pegasus 场景层级示例，按实际业务口径调整/扩充
INSERT INTO scene_taxonomy (id, parent_id, level, code, name_cn) VALUES
    (1, NULL, 1, 'highway',      '高速公路'),
    (2, NULL, 1, 'urban_road',   '城市道路'),
    (3, NULL, 1, 'rural_road',   '乡村道路'),
    (4, 1,    2, 'highway_toll', '高速收费站'),
    (5, 1,    2, 'highway_ramp', '高速匝道'),
    (6, 2,    2, 'urban_intersection', '城市路口'),
    (7, 2,    2, 'urban_tunnel',       '城市隧道');
SELECT setval('scene_taxonomy_id_seq', (SELECT max(id) FROM scene_taxonomy));

CREATE TABLE video_scene_tags (
    id            BIGSERIAL PRIMARY KEY,
    video_sha256  BYTEA NOT NULL REFERENCES videos(sha256) ON DELETE CASCADE,
    taxonomy_id   INT NOT NULL REFERENCES scene_taxonomy(id),
    confidence    NUMERIC(5,4),             -- 若为算法自动打标，记录置信度
    source        VARCHAR(16) NOT NULL DEFAULT 'manual',  -- manual/auto
    tagged_by     INT REFERENCES operators(id),
    tagged_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (video_sha256, taxonomy_id)
);

CREATE INDEX idx_scene_tags_video ON video_scene_tags(video_sha256);

-- ============================================================
-- 4. 标注（数据量大，JSON只存地址）
-- ============================================================

CREATE TABLE annotation_batches (
    id                BIGSERIAL PRIMARY KEY,
    video_sha256      BYTEA NOT NULL REFERENCES videos(sha256) ON DELETE CASCADE,
    annotation_type   VARCHAR(32) NOT NULL,   -- bbox/segmentation/keypoint/lane...
    format            VARCHAR(32),            -- COCO/YOLO/自定义
    storage_path      TEXT NOT NULL,          -- json文件(或分片目录)存储地址
    frame_start       INT,
    frame_end         INT,
    object_count      INT,
    file_size_bytes   BIGINT,
    checksum          BYTEA,                  -- json内容的sha256，校验完整性
    version           INT NOT NULL DEFAULT 1,
    status            VARCHAR(16) NOT NULL DEFAULT 'draft',  -- draft/reviewed/approved
    annotator_id      INT REFERENCES operators(id),
    reviewer_id       INT REFERENCES operators(id),
    annotated_at      TIMESTAMPTZ,
    reviewed_at       TIMESTAMPTZ,
    metadata          JSONB NOT NULL DEFAULT '{}',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN annotation_batches.storage_path IS '标注JSON文件(或分片目录)的对象存储地址，JSON本体不入库';
COMMENT ON COLUMN annotation_batches.checksum IS 'JSON内容的sha256，用于校验文件完整性/是否被篡改';
COMMENT ON COLUMN annotation_batches.status IS 'draft=标注中, reviewed=已审核待确认, approved=审核通过(触发器会据此把进度表置为completed)';

CREATE INDEX idx_annotation_video ON annotation_batches(video_sha256);
CREATE INDEX idx_annotation_status ON annotation_batches(status);

-- ============================================================
-- 5. 推理
-- ============================================================

CREATE TABLE inference_runs (
    id                   BIGSERIAL PRIMARY KEY,
    video_sha256         BYTEA NOT NULL REFERENCES videos(sha256) ON DELETE CASCADE,
    model_name           VARCHAR(128) NOT NULL,
    model_version        VARCHAR(32),
    task_type            VARCHAR(32),          -- detection/classification/segmentation
    output_storage_path  TEXT NOT NULL,
    object_count         INT,
    avg_confidence       NUMERIC(5,4),
    compute_node         VARCHAR(64),
    run_by               INT REFERENCES operators(id),
    started_at           TIMESTAMPTZ,
    finished_at          TIMESTAMPTZ,
    status               VARCHAR(16) NOT NULL DEFAULT 'completed',
    params                JSONB NOT NULL DEFAULT '{}',   -- 推理参数，自由扩展
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_inference_video ON inference_runs(video_sha256);

-- ============================================================
-- 6. 追踪
-- ============================================================

CREATE TABLE tracking_runs (
    id                   BIGSERIAL PRIMARY KEY,
    video_sha256         BYTEA NOT NULL REFERENCES videos(sha256) ON DELETE CASCADE,
    inference_run_id     BIGINT REFERENCES inference_runs(id),   -- 追踪基于哪次推理结果
    algorithm            VARCHAR(64),           -- ByteTrack/DeepSORT...
    output_storage_path  TEXT NOT NULL,
    track_count          INT,
    run_by               INT REFERENCES operators(id),
    started_at           TIMESTAMPTZ,
    finished_at          TIMESTAMPTZ,
    status               VARCHAR(16) NOT NULL DEFAULT 'completed',
    params               JSONB NOT NULL DEFAULT '{}',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_tracking_video ON tracking_runs(video_sha256);

-- ============================================================
-- 7. 生成数据（汇总各阶段产出的最终数据集）
-- ============================================================

CREATE TABLE generated_datasets (
    id                 BIGSERIAL PRIMARY KEY,
    video_sha256       BYTEA NOT NULL REFERENCES videos(sha256) ON DELETE CASCADE,
    dataset_type       VARCHAR(64),           -- 训练集/测试集/导出格式等
    format             VARCHAR(32),
    storage_path       TEXT NOT NULL,
    source_stage_refs  JSONB NOT NULL DEFAULT '{}',  -- 记录来源，如 {"annotation_batch_ids":[1,2],"tracking_run_id":5}
    generated_by       INT REFERENCES operators(id),
    generated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    status             VARCHAR(16) NOT NULL DEFAULT 'completed',
    metadata           JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_dataset_video ON generated_datasets(video_sha256);

-- ============================================================
-- 8. 场景提取（cut-in / cut-out 等事件，类型可扩展）
-- ============================================================

CREATE TABLE event_types (
    id          SERIAL PRIMARY KEY,
    code        VARCHAR(32) UNIQUE NOT NULL,   -- cut_in / cut_out / lane_change ...
    name_cn     VARCHAR(64) NOT NULL,
    description TEXT
);

INSERT INTO event_types (code, name_cn, description) VALUES
    ('cut_in',  '切入', '目标车辆切入本车道'),
    ('cut_out', '切出', '目标车辆切出本车道');

CREATE TABLE scene_events (
    id               BIGSERIAL PRIMARY KEY,
    video_sha256     BYTEA NOT NULL REFERENCES videos(sha256) ON DELETE CASCADE,
    event_type_id    INT NOT NULL REFERENCES event_types(id),
    tracking_run_id  BIGINT REFERENCES tracking_runs(id),
    track_object_id  VARCHAR(64),           -- 对应追踪结果里的目标ID
    start_frame      INT,
    end_frame        INT,
    start_time_s     NUMERIC(10,3),
    end_time_s       NUMERIC(10,3),
    confidence       NUMERIC(5,4),
    extracted_by     VARCHAR(64),           -- 算法名或人工
    extracted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata         JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_scene_events_video ON scene_events(video_sha256);
CREATE INDEX idx_scene_events_type ON scene_events(event_type_id);

-- ============================================================
-- 9. 可视化支持（预览图/缩略图等，供前端看板使用）
-- ============================================================

CREATE TABLE video_previews (
    id            BIGSERIAL PRIMARY KEY,
    video_sha256  BYTEA NOT NULL REFERENCES videos(sha256) ON DELETE CASCADE,
    preview_type  VARCHAR(32) NOT NULL,     -- thumbnail/gif/keyframe_grid
    storage_path  TEXT NOT NULL,
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata      JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_preview_video ON video_previews(video_sha256);

-- ============================================================
-- 10. 通用: updated_at 自动维护
-- ============================================================

CREATE OR REPLACE FUNCTION fn_touch_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_videos_touch BEFORE UPDATE ON videos
FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();

CREATE TRIGGER trg_vps_touch BEFORE UPDATE ON video_pipeline_status
FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();

-- ============================================================
-- 11. 明细表写入 -> 同步 video_pipeline_status
--
--  【设计变更】此前用DB触发器做同步，现在项目引入Python服务层
--  (FastAPI + SQLAlchemy)后改由Python侧显式更新，原因：
--    1. 校验规则、飞书升级提醒这些容易变的业务逻辑本来就要放Python，
--       状态同步跟它们是同一条链路，拆两处（DB触发器 + Python）反而
--       容易出现"数据库说完成了，但飞书没通知到"这种不一致
--    2. Python里更容易写单测/加日志/加审计(data_submissions)
--    3. 学生用FastAPI接口写入数据时，校验失败要能立刻拦截并阻止状态
--       变成completed，这个"先校验再更新状态"的顺序用DB触发器不好表达
--
--  下面的函数/触发器保留但默认不创建(CREATE TRIGGER已注释)，
--  只是留作参考实现或极端情况下的DB兜底方案。
--  Python侧等价实现见 app/services/pipeline.py 的 sync_stage_status()
-- ============================================================

CREATE OR REPLACE FUNCTION fn_sync_stage_status() RETURNS TRIGGER AS $$
DECLARE
    v_stage_code TEXT := TG_ARGV[0];
    v_new_status stage_status := TG_ARGV[1]::stage_status;
BEGIN
    UPDATE video_pipeline_status
    SET status = v_new_status,
        ref_table = TG_TABLE_NAME,
        ref_id = NEW.id,
        finished_at = CASE WHEN v_new_status = 'completed' THEN now() ELSE finished_at END,
        started_at = COALESCE(started_at, now()),
        updated_at = now()
    WHERE video_sha256 = NEW.video_sha256
      AND stage_id = (SELECT id FROM pipeline_stages WHERE code = v_stage_code);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- CREATE TRIGGER trg_annotation_in_progress
-- AFTER INSERT ON annotation_batches
-- FOR EACH ROW EXECUTE FUNCTION fn_sync_stage_status('annotation', 'in_progress');

CREATE OR REPLACE FUNCTION fn_sync_stage_status_helper(
    p_video_sha256 BYTEA, p_ref_id BIGINT, p_ref_table TEXT,
    p_stage_code TEXT, p_status stage_status
) RETURNS VOID AS $$
BEGIN
    UPDATE video_pipeline_status
    SET status = p_status,
        ref_table = p_ref_table,
        ref_id = p_ref_id,
        finished_at = CASE WHEN p_status = 'completed' THEN now() ELSE finished_at END,
        updated_at = now()
    WHERE video_sha256 = p_video_sha256
      AND stage_id = (SELECT id FROM pipeline_stages WHERE code = p_stage_code);
END;
$$ LANGUAGE plpgsql;

-- CREATE TRIGGER trg_annotation_approved ...  (同上，改由Python在校验通过后显式调用)
-- CREATE TRIGGER trg_inference_completed ...
-- CREATE TRIGGER trg_tracking_completed ...
-- CREATE TRIGGER trg_dataset_completed ...

COMMENT ON FUNCTION fn_sync_stage_status IS '[备用/未启用] 状态同步的DB兜底实现，主逻辑现在在Python服务层';

-- ============================================================
-- 12. 扫盘 / 通知 / 提交审计 / 事件环境维度
--     (为Python后端层准备的表：扫盘服务、飞书通知服务、
--      学生提交校验服务都直接读写这几张表)
-- ============================================================

-- 12.1 扫盘任务与发现记录
CREATE TABLE scan_jobs (
    id             BIGSERIAL PRIMARY KEY,
    root_path      TEXT NOT NULL,          -- 本次扫描的根目录/挂载点
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ,
    files_found    INT DEFAULT 0,
    files_new      INT DEFAULT 0,          -- 新增(触发新任务流)数量
    files_duplicate INT DEFAULT 0,         -- 内容已存在(sha256重复)数量
    status         VARCHAR(16) NOT NULL DEFAULT 'running', -- running/completed/failed
    error_message  TEXT
);
COMMENT ON TABLE scan_jobs IS '每次扫盘的批次记录，Python扫盘服务负责写入';

CREATE TABLE scan_discoveries (
    id                    BIGSERIAL PRIMARY KEY,
    scan_job_id           BIGINT NOT NULL REFERENCES scan_jobs(id) ON DELETE CASCADE,
    file_path             TEXT NOT NULL,
    file_size_bytes       BIGINT,
    hash_status           VARCHAR(16) NOT NULL DEFAULT 'pending', -- pending/hashing/done/error
    sha256                BYTEA,                   -- 算完才填
    resolved_video_sha256 BYTEA REFERENCES videos(sha256),  -- 落库后指向videos
    is_new_task           BOOLEAN,                 -- true=新sha256(新任务流)，false=已存在(仅记录/可能是新路径)
    error_message         TEXT,
    discovered_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_scan_discoveries_job ON scan_discoveries(scan_job_id);
CREATE INDEX idx_scan_discoveries_hash_status ON scan_discoveries(hash_status);
COMMENT ON TABLE scan_discoveries IS '扫盘发现的每个文件一行；sha256算完后决定是插入videos新任务还是仅关联已有记录';

-- 12.2 通知（飞书等），状态机由Python后台任务驱动
CREATE TABLE notifications (
    id                 BIGSERIAL PRIMARY KEY,
    video_sha256        BYTEA REFERENCES videos(sha256),
    stage_id            INT REFERENCES pipeline_stages(id),
    event_type          VARCHAR(32) NOT NULL,   -- new_task/stage_failed/validation_escalation/...
    target_operator_id  INT REFERENCES operators(id),
    channel             VARCHAR(16) NOT NULL DEFAULT 'feishu',
    payload             JSONB NOT NULL DEFAULT '{}',
    status               VARCHAR(16) NOT NULL DEFAULT 'pending', -- pending/sent/failed
    retry_count          INT NOT NULL DEFAULT 0,
    sent_at              TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_notifications_status ON notifications(status);
COMMENT ON TABLE notifications IS '通知发送队列：应用层插pending行，后台worker轮询发送飞书消息并回写status/sent_at';

-- 12.3 学生数据提交审计（配合校验失败升级提醒）
CREATE TABLE data_submissions (
    id                  BIGSERIAL PRIMARY KEY,
    submitted_by        INT NOT NULL REFERENCES operators(id),
    target_table        VARCHAR(64) NOT NULL,   -- annotation_batches/scene_events/...
    target_ref_id       BIGINT,
    payload              JSONB,
    validation_status    VARCHAR(16) NOT NULL,  -- passed/failed
    validation_errors    JSONB,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_submissions_operator ON data_submissions(submitted_by, created_at DESC);
COMMENT ON TABLE data_submissions IS '每次FastAPI写入前的校验记录；同一人连续failed超阈值由Python服务触发notifications升级提醒';

-- 12.4 场景事件的环境维度（支持"cut-in发生在雨天多少次"这类统计）
CREATE TABLE weather_types (
    id       SERIAL PRIMARY KEY,
    code     VARCHAR(32) UNIQUE NOT NULL,  -- sunny/rainy/foggy/snowy/overcast
    name_cn  VARCHAR(32) NOT NULL
);
INSERT INTO weather_types (code, name_cn) VALUES
    ('sunny','晴'), ('rainy','雨'), ('foggy','雾'), ('snowy','雪'), ('overcast','阴');

ALTER TABLE scene_events ADD COLUMN weather_id INT REFERENCES weather_types(id);
ALTER TABLE scene_events ADD COLUMN lighting VARCHAR(16);       -- day/night/dusk
ALTER TABLE scene_events ADD COLUMN road_condition VARCHAR(16); -- dry/wet/icy
COMMENT ON COLUMN scene_events.weather_id IS '事件发生时的天气，打在事件级而非视频级，因为一段视频里天气可能变化';

-- ============================================================
-- 使用示例
-- ============================================================

-- 1) 入库一个新视频（触发器会自动为它创建8个阶段的pending状态行）
-- INSERT INTO videos (sha256, original_name, storage_path, collected_at)
-- VALUES (decode('a3f5...64位hex...', 'hex'), 'DJI_0001.mp4', 'oss://bucket/raw/DJI_0001.mp4', now());

-- 2) 更新"标注"阶段进度
-- UPDATE video_pipeline_status
-- SET status = 'in_progress', progress_percent = 40, started_at = now(), operator_id = 3
-- WHERE video_sha256 = decode('a3f5...','hex')
--   AND stage_id = (SELECT id FROM pipeline_stages WHERE code='annotation');

-- 3) 查询某个视频的整体进展（前端看板直接用这条）
-- SELECT * FROM v_video_progress WHERE sha256 = decode('a3f5...','hex');

-- 4) 查询所有"标注"阶段未完成的视频
-- SELECT v.original_name, vps.status, vps.progress_percent
-- FROM video_pipeline_status vps
-- JOIN videos v ON v.sha256 = vps.video_sha256
-- JOIN pipeline_stages ps ON ps.id = vps.stage_id
-- WHERE ps.code = 'annotation' AND vps.status != 'completed';

-- 5) 新增一个流水线阶段(比如未来加"质检"阶段)，无需改任何表结构
-- INSERT INTO pipeline_stages (code, name_cn, stage_order, description)
-- VALUES ('qc_review', '质检', 85, '标注质量抽检');
-- （新增后需要为已有视频批量补一行 video_pipeline_status，可写一次性脚本）
