*[中文版](README.md)*

# Aerial Survey Data Management System

Manages the full lifecycle of aerial survey video data — from collection, annotation,
inference, and tracking through to dataset generation and scene extraction — replacing a
previous workflow that relied on verbal coordination, had no visibility into progress, and
could not trace data provenance.

## Status

| Module                                                        | Status                                  |
| --------------------------------------------------------------| ---------------------------------------- |
| Database design                                                | ✅ Done, covers the full video pipeline  |
| Automatic disk scanning                                        | ✅ Implemented                           |
| Feishu task notifications                                      | ⬜ Queue mode not yet implemented        |
| Data submission validation + failure escalation alerts         | 🟡 Skeleton in place, filling in by type |
| ORM/API for per-stage assets (annotation/inference/tracking/dataset/scene extraction) | 🟡 Skeleton in place, filling in by type |
| Map data pipeline                                               | ⬜ On hold, hooks reserved, timing TBD   |
| Frontend                                                        | ⬜ Not started, backend first per plan   |

## What this system solves

- **Progress visibility**: which stage each video is in, who's working on it, and when it
  finished — all queryable in one place, no need to ask around
- **Clear permissions**: different roles (admin/annotator/pilot, etc.) have separate
  permissions; no shared accounts
- **Automatic escalation**: repeated validation failures on a data submission automatically
  notify admins — no need for anyone to watch it manually
- **No duplicates, no data loss**: every video is deduplicated by content hash, and every
  step of the processing chain is recorded and traceable

## Tech stack

- PostgreSQL 14+ (`aerial` schema)
- Python + FastAPI + SQLAlchemy (async) + APScheduler (scheduled jobs)
- Feishu custom bot webhook for notifications

## Connecting to the database

The tables live under the `aerial` schema (not the default `public` one) — keep this in
mind for all the methods below.

Students/external collaborators should use the read-only account `reader` (ask the project
lead for the password — it's not checked into the repo); it can only query, never modify
data. The examples below all use this account. Do not use the admin account to connect.

### 1. Shell (psql)

```bash
psql "postgresql://reader:<password>@localhost:5432/aerial_db"
```

```sql
SET search_path TO aerial, public;   -- run this first after connecting; no schema prefix needed afterward
\dt                                   -- list all tables
\d videos                             -- inspect the videos table structure
SELECT * FROM videos LIMIT 5;
```

One-off query (without entering interactive mode):

```bash
psql "postgresql://reader:<password>@localhost:5432/aerial_db" \
  -c "SET search_path TO aerial, public;" \
  -c "SELECT storage_path, created_at FROM videos ORDER BY created_at DESC LIMIT 5;"
```

### 2. Python

```python
import asyncio
import asyncpg

async def main():
    conn = await asyncpg.connect(
        host="localhost", port=5432, user="reader", password="<password>", database="aerial_db",
        server_settings={"search_path": "aerial,public"},
    )
    rows = await conn.fetch("SELECT storage_path, created_at FROM videos LIMIT 5;")
    for r in rows:
        print(dict(r))
    await conn.close()

asyncio.run(main())
```

Common query examples (swap the SQL in `conn.fetch(...)` above for either of these):

```sql
-- 1. Videos shorter than 5 seconds
SELECT storage_path, duration_seconds FROM videos WHERE duration_seconds < 5;

-- 2. Videos missing GPS coordinates (either longitude or latitude is NULL)
SELECT storage_path FROM videos WHERE collect_lon IS NULL OR collect_lat IS NULL;
```

Requires: `pip install asyncpg`

### 3. Metabase (no code — point-and-click or write SQL in the browser)

[Metabase](https://www.metabase.com/) is an open-source BI tool. Once connected to the
database, you can build charts and run ad-hoc SQL queries in the browser without writing
Python/psql — good for anyone who just wants to look at the data without touching code.
Same caveat applies here: the default schema is `public`, so when picking a
database/table in Metabase, select tables under the `aerial` schema, or change `public` to
`aerial` in the connection's advanced settings.

Access URL: `<server-ip>:3000`; ask quanwei/ziyu for the login credentials.

### `videos` table field reference

One row per video file. `sha256` is a content hash — the same content (even under a
different path/filename) will only ever produce one record.

| Field                          | Meaning                                                                                                         |
| ------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `sha256`                        | SHA256 hash of the video content (32 bytes), primary key, used for content-based deduplication                  |
| `original_name`                 | Original file name                                                                                               |
| `storage_path`                  | Absolute path on the server/NAS                                                                                  |

> If `storage_path` starts with `/mnt/Expansion2-shared/...`: that's the bindfs path, used specifically by the Aliyun download pipeline (the download tool calls `chmod`, which exFAT doesn't support — bindfs swallows that call). It has nothing to do with reading files, but reading still goes through an extra FUSE hop — measured: the same already-cached data hits 2.9GB/s read directly vs. 2.8MB/s through this path, a ~1000x difference. **When manually browsing/copying data, swap `Expansion2-shared` for `Expansion2`** — same content, much faster: `path.replace("Expansion2-shared", "Expansion2")`, or just use `aerial_backend/scripts/fast_path.py` (supports more such mappings, configured in `path_aliases.json`, which isn't committed to git — first time, run `cp path_aliases.json.example path_aliases.json`).
| `file_size_bytes`                | File size in bytes                                                                                               |
| `duration_seconds`               | Video duration in seconds                                                                                        |
| `width` / `height`               | Resolution                                                                                                       |
| `fps`                            | Frame rate                                                                                                       |
| `codec`                          | Codec (e.g. h264, hevc)                                                                                          |
| `collected_at`                   | Capture time (prefers the video's own creation-time metadata; falls back to file mtime if absent)                |
| `collect_lon` / `collect_lat`    | Capture location longitude/latitude (from the same-named SRT telemetry subtitle file; empty if no SRT file)      |
| `replaces_sha256`                | If this video is a re-encoded/re-compressed new version of an older video at the same location, this points to the old `sha256` it replaces; empty for ordinary new videos |
| `created_at` / `updated_at`      | When this row itself was created/updated (not the video's shoot time)                                            |
| `deleted_at`                     | Non-null means this record has been retired (usually because the video at this location was replaced with a new version) |

**Don't combine re-encoding (transcoding) and relocation in the same operation.** Disk
scanning correctly tells these two cases apart using two distinct, exact-match signals:
"content unchanged, path changed" (a move) and "path unchanged, content changed" (a
re-encode). If a file is both moved to a new path *and* re-encoded at the same time (new
path + new content), the system has no reliable way to determine which old record the new
file corresponds to — guessing from heuristics like filename or size is unreliable, and a
wrong guess is worse than no association at all. The result would be: the old record never
gets retired, and the new file gets ingested as a completely unrelated new video, losing the
`replaces_sha256` lineage. The correct approach is two separate steps: first move the file to
its new location (content unchanged — the system will automatically detect this as a
"whole-folder move" and send a notification), confirm the move took effect, and only then
re-encode it in place at the new location.

## Urgent Download Tool (skip the stability wait, download + ingest immediately)

Normally, a newly-uploaded Aliyun folder has to wait for "no change across several
consecutive heartbeats" (usually a few days) before the system considers it "fully
uploaded" and downloads it automatically. If you have an urgent need (e.g. a client
needs a specific folder processed right now) and can't wait those few days, use this
script instead:

```bash
cd aerial_backend
uv run python scripts/urgent_download.py \
    --session-name shanghai_jianchuanroad_mavic4pro_120m_wangxiangyu_20261104
```

`--session-name` must be the complete session folder name (format
`city_site_drone_height_person_date`; the drone model has to be in the whitelist
`SESSION_FOLDER_DEVICES` in `app/services/session_naming.py` — ask an admin to add a
new model if needed), and that folder must sit at the **root** of a cloud drive (not
nested inside another folder). If either check fails, the command exits immediately
with a clear terminal error — nothing gets downloaded, no messages get sent.

Once validation passes, the command hands control of the terminal back to you right
away (no need for `&` or `nohup` yourself) — the actual download runs independently in
the background. A Feishu message is sent to the group on both success and failure. If
you don't hear back within, say, half an hour, just re-run the exact same command —
this script is designed to be safely re-run: already-downloaded files won't be
re-transferred, and already-ingested videos won't end up duplicated in the database.

If you'd rather not SSH into the server, you can hit the same thing over HTTP instead —
same validation, same background job:

```bash
curl -X POST "http://<server-address>:8088/aliyun/urgent-download?session_name=shanghai_jianchuanroad_mavic4pro_120m_wangxiangyu_20261104"
```

A bad format returns 422 with the specific problem; a folder not found at any drive
root returns 404; a 200 means validation passed, with the background log path in the
response body — the actual result still comes through Feishu.

**This tool skips the "stability wait" safety net.** That means it might grab a cloud
folder that's still mid-upload — this is a deliberate tradeoff of "urgent" mode, not a
bug, and there's no way around it. If more files show up in the cloud afterward, the
regular automated pipeline will detect that and alert via Feishu, but there's no
guarantee the first grab was complete.

## Downstream Pipeline Artifact Registration

**One-line summary**: copy the single file
`aerial_backend/tools/artifact_registration.py` into your own repo (or
ask an admin for it), `from artifact_registration import register_artifact`,
and when calling it, fill in at least `stage_code` (ask an admin for this), `input_video_paths`
(the raw video path(s) you used this run), and `output_path` (the file you produced this run).

Downstream stages (perception/tracking, dataset generation, SOTIF scene
extraction, etc.) each live in their own independently-maintained repo,
updated on their own schedule — not a shared codebase. To know which raw
video an artifact came from and which code version produced it, there's a
generic registration endpoint.

### Plain HTTP

```bash
curl -X POST "http://<server-address>:8088/pipeline/artifacts" \
  -H "Content-Type: application/json" \
  -d '{
    "stage_code": "ad_perception_tracking",
    "input_video_paths": ["/mnt/Expansion2/.../xxx.MP4"],
    "output_path": "/mnt/.../xxx_STD_TRK.csv",
    "code_repo": "AD_pipeline_1",
    "code_commit": "a1b2c3d...",
    "code_dirty": false,
    "metadata": {"detection_model": "yolov8"},
    "operator_name": "quanwei"
  }'
```

| Field | Required | Notes |
|---|---|---|
| `stage_code` | yes | Maps to `pipeline_stages.code` — ask an admin to add a row for a new stage |
| `input_video_paths` | yes | Absolute path(s) of the raw video(s); the backend resolves sha256 via `videos.storage_path` — callers never compute a video hash themselves |
| `output_path` | yes | Absolute path of this run's main output file/directory |
| `code_repo`/`code_commit`/`code_dirty` | no | Code version info |
| `metadata` | no | Free-form JSON for whatever the stage wants to record (model used, param fingerprint, etc.) |
| `operator_name` | no | Who ran it |

If even one path in `input_video_paths` doesn't resolve against `videos`,
the whole request 422s, naming the offending path(s) — never a half-done
state where some videos registered and others didn't.

### Python client (for `nds_code`-style student repos)

`aerial_backend/tools/artifact_registration.py` is a single, fully self-contained
file (stdlib only, no dependencies to install) — copy it into your own
repo and call it:

```python
from artifact_registration import register_artifact

register_artifact(
    "ad_perception_tracking",
    [raw_video_path],
    output_path,
    {"detection_model": "yolov8"},
)
```

The git commit is auto-detected at call time — you don't pass it
yourself. A network/backend outage never raises or interrupts your
pipeline: the failed request is appended to a local pending-retry file
(`~/.aerial_pipeline/pending_artifacts.jsonl`), and every subsequent
`register_artifact` call tries to flush it first — nothing to remember to
do manually. You can also run
`python -m artifact_registration --retry-pending` to flush
it on demand.

**Backfilling old data produced before this feature existed**: for a
normal call, don't pass `code_commit_override` (it auto-detects the
current code version). For genuinely old data where nobody remembers the
commit, pass `code_commit_override=None` explicitly rather than guessing
— this honestly leaves the code version blank instead of silently filling
in "whatever's checked out right now" (which would be misleading, not
just imprecise). If you roughly remember a commit (even just the first
few characters), pass that — it's resolved and verified via git's own
short-hash resolution; if it doesn't resolve, a warning is logged and the
field is left blank, but the artifact still registers.
