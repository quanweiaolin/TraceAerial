SET search_path TO aerial, public;

INSERT INTO pipeline_stages (code, name_cn, stage_order, is_active, description)
VALUES ('ad_perception_tracking', '感知跟踪处理', 10, true,
        'AD_pipeline_1: 检测+稳像+对齐+跟踪+后处理+标准化输出，合并登记为一个stage')
ON CONFLICT (code) DO NOTHING;

CREATE TABLE IF NOT EXISTS pipeline_artifacts (
    id                   bigserial PRIMARY KEY,
    stage_code           text NOT NULL REFERENCES pipeline_stages(code),
    input_video_sha256   bytea[] NOT NULL,
    code_repo            text,
    code_commit          text,
    code_dirty           boolean,
    output_path          text NOT NULL,
    upstream_artifact_id bigint REFERENCES pipeline_artifacts(id),
    metadata             jsonb NOT NULL DEFAULT '{}',
    operator_name        text,
    created_at           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pipeline_artifacts_input_video_sha256
    ON pipeline_artifacts USING gin (input_video_sha256);
