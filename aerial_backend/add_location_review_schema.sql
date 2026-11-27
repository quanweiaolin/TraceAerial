-- 2026-11-25加：这份迁移必须在重启跑这份代码的app进程之前执行完。
-- app/models.py这次给videos/locations/operators三张表新增了ORM映射列
-- (location_confirmed等)，一旦新代码跑起来而这几列还不存在，针对这几张
-- 表的任何查询(包括跟这次改动完全无关的现有接口)都会因为
-- UndefinedColumnError直接报错，不是只影响地址复核相关的新接口。

SET search_path TO aerial, public;

CREATE TABLE IF NOT EXISTS location_groups (
    id           bigserial PRIMARY KEY,
    name         text NOT NULL,
    confirmed_by integer REFERENCES operators(id),
    confirmed_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE locations
    ADD COLUMN IF NOT EXISTS location_group_id bigint REFERENCES location_groups(id),
    ADD COLUMN IF NOT EXISTS representative_frame_path text;

ALTER TABLE videos
    ADD COLUMN IF NOT EXISTS location_confirmed boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS location_confirmed_by integer REFERENCES operators(id),
    ADD COLUMN IF NOT EXISTS location_confirmed_at timestamptz;

ALTER TABLE operators
    ADD COLUMN IF NOT EXISTS can_review_location boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS operator_api_tokens (
    id          bigserial PRIMARY KEY,
    operator_id integer NOT NULL REFERENCES operators(id),
    token_hash  text NOT NULL UNIQUE,
    created_at  timestamptz NOT NULL DEFAULT now(),
    revoked_at  timestamptz
);

CREATE TABLE IF NOT EXISTS location_review_items (
    id                     bigserial PRIMARY KEY,
    video_sha256           bytea NOT NULL REFERENCES videos(sha256),
    candidate_location_id  bigint REFERENCES locations(id),
    screenshot_path        text,
    visual_match_result    jsonb,
    llm_verdict            jsonb,
    llm_verdict_at         timestamptz,
    human_decision         text CHECK (human_decision IN ('same_location', 'different_location', 'new_location')),
    decided_by             integer REFERENCES operators(id),
    decided_at             timestamptz,
    status                 text NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'decided', 'screenshot_failed')),
    created_at             timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS location_change_history (
    id          bigserial PRIMARY KEY,
    entity_type text NOT NULL CHECK (entity_type IN ('video', 'location', 'location_group')),
    entity_id   text NOT NULL,
    field_name  text NOT NULL,
    old_value   text,
    new_value   text,
    changed_by  integer REFERENCES operators(id),
    changed_at  timestamptz NOT NULL DEFAULT now(),
    reason      text
);

CREATE INDEX IF NOT EXISTS idx_location_review_items_status
    ON location_review_items (status);
CREATE INDEX IF NOT EXISTS idx_location_review_items_video_sha256
    ON location_review_items (video_sha256);
CREATE INDEX IF NOT EXISTS idx_location_change_history_entity
    ON location_change_history (entity_type, entity_id, field_name);

ALTER TABLE locations
    ADD COLUMN IF NOT EXISTS representative_video_sha256 bytea REFERENCES videos(sha256);

ALTER TABLE location_groups
    ADD COLUMN IF NOT EXISTS aliases jsonb NOT NULL DEFAULT '{}';

-- 2026-11-27加：命名审核告警表，见app/services/location_naming_audit.py。
CREATE TABLE IF NOT EXISTS location_naming_alerts (
    id                 bigserial PRIMARY KEY,
    alert_type         text NOT NULL CHECK (alert_type IN ('name_drift', 'merge_candidate')),
    city               text NOT NULL,
    location_ids       bigint[] NOT NULL,
    raw_site_votes     jsonb NOT NULL,
    votes_fingerprint  text NOT NULL,
    suggested_value    text,
    status             text NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'confirmed', 'dismissed')),
    resolved_by        integer REFERENCES operators(id),
    resolved_at        timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_location_naming_alerts_dedupe
    ON location_naming_alerts (alert_type, location_ids, votes_fingerprint);
