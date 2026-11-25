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
    entity_id   bigint NOT NULL,
    field_name  text NOT NULL,
    old_value   text,
    new_value   text,
    changed_by  integer REFERENCES operators(id),
    changed_at  timestamptz NOT NULL DEFAULT now(),
    reason      text
);
