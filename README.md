# IG Funnel - Instagram content processing and posting

Admin uploads videos via the dashboard. The backend processes them with FFmpeg,
posts them to Instagram Reels on schedule (Celery Beat). All configuration lives in the dashboard UI. The Instagram bio can link to an external Telegram bot.

## Quick start (Docker)

```bash
cp .env.example .env
# edit .env: SECRET_KEY, FERNET_KEY, ADMIN_PASSWORD, DATABASE_URL…
docker compose up --build
```

- Default deploy includes nginx (the only ingress): everything on
  `${NGINX_PORT:-8080}` — `/` → frontend, `/api/*` + `/ws` + docs → backend,
  same-origin (no CORS). The frontend image is baked for this (empty
  `NEXT_PUBLIC_API_URL`, internal rewrite to `http://backend:8000`).
  `CLIENT_MAX_BODY_SIZE` must stay above the backend's `MAX_UPLOAD_MB`
  (defaults: 550m vs 500) or nginx 413s uploads.
- Only set `FRONTEND_API_URL` when exposing the frontend container directly
  (no nginx) — it is baked into the client bundle at build time.
- API docs (`/docs`, `/openapi.json`) are disabled by default for security —
  set `DOCS_ENABLED=true` to expose them (local dev only).
- Login with `ADMIN_USERNAME` / `ADMIN_PASSWORD` from `.env`.

## Services

| Service  | Role |
|----------|------|
| backend  | FastAPI + Uvicorn (port 8000) |
| worker-fast | Celery worker, fast lane: scheduler ticks, watchdog, heartbeats, proxy checks |
| worker-slow | Celery worker, slow lane: FFmpeg processing, uploads, source ingest, analytics |
| beat     | Celery Beat (per-minute scheduler, analytics 4h, bio rotation, proxy checks, cleanup) |
| redis    | Broker + result backend + realtime pub/sub + progress keys |
| frontend | Next.js 14 dashboard |
| postgres | Optional (`--profile postgres`) — see "Database → PostgreSQL" |

Default is SQLite (`./data/app.db` bind-mounted from the repo root) — zero-config.

### Task lanes

Both workers run `--pool=solo` (one task at a time per process). Without
lanes, a 10-minute FFmpeg render would stall the per-minute scheduler ticks
queued behind it — late posts and phantom "Scheduler was down" watchdog
gaps. So tasks are routed to two queues (`backend/app/tasks/celery_app.py`:
`TASK_FAST_QUEUE`/`TASK_SLOW_QUEUE`, `SLOW_TASKS`): `process_video`,
`execute_post`, `ingest_source` and `fetch_all_analytics` go to `slow`;
everything else (scheduler, watchdog, heartbeats, proxy checks, cleanup)
stays on `fast`. Each compose worker consumes exactly one queue
(`-Q fast` / `-Q slow`) and sets `WORKER_LANE` so health signals stay
per-lane. Never set a `cpus`/`mem_limit` above the smallest host you
deploy to (a 1-vCPU box rejects `cpus: 2.0` at container-create time).

### Anti-detection & reliability

- **Human-like posting jitter**: each fired slot gets a random 0–N minute
  offset (setting `post_jitter_minutes`, default 5) with second resolution —
  the post is dispatched to the slow lane via `apply_async(countdown=…)`
  at the exact second, not rounded to the next minute tick.
- **Exactly-once dispatch**: every post created by the tick is stamped
  `dispatched_at`; the per-minute backstop atomically
  (`UPDATE … WHERE … RETURNING`) picks up only unstamped posts (manual/API
  schedules) or stamps older than `dispatch_stale_minutes` (default 30 —
  re-dispatched, never lost). The atomic `claim_post` guarantees no
  double-upload even if two tasks ever race.
- **Action-block detection**: `feedback_required` from Instagram → account
  goes to `cooldown` for `action_block_cooldown_hours` (default 24), proxy
  rotates to a proven spare if one exists, and a dedicated warning
  notification fires (deduped daily). No pointless retries against a block.
- **Shadowban scan** (every 6h, `shadowban_scan_enabled`): compares the
  median 24h views of recent posts against the median 7d views of the
  account's baseline — a collapse below `shadowban_collapse_ratio`
  (default 0.10) pauses the account for `shadowban_pause_hours` (default
  48) and notifies; recovery auto-resumes only cooldowns that match the
  shadowban episode, never manual pauses. Tunables:
  `shadowban_min_baseline_views`, `shadowban_min_recent_posts`.

## Local dev (no Docker)

Backend needs Python 3.11+, FFmpeg, and Redis:

```bash
cd backend
pip install -r requirements.txt
cp ../.env.example ../.env   # or set env vars
uvicorn app.main:app --reload
# Two lane workers (see "Task lanes" below): the fast lane runs scheduler
# ticks/watchdog, the slow lane FFmpeg/uploads/ingest/analytics. For a
# quick single-worker dev setup, drain both queues with -Q fast,slow and
# leave WORKER_LANE unset.
WORKER_LANE=fast celery -A app.tasks.celery_app.celery worker --loglevel=info --pool=solo -Q fast --hostname=fast
WORKER_LANE=slow celery -A app.tasks.celery_app.celery worker --loglevel=info --pool=solo -Q slow --hostname=slow
celery -A app.tasks.celery_app.celery beat --loglevel=info
alembic upgrade head          # REQUIRED on existing DBs — the startup schema gate refuses to boot when the DB is behind the code
```

Frontend:

```bash
cd frontend
npm install
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev
```

## First-run checklist

1. Log in → **Accounts** → add IG accounts (one proxy per account recommended).
2. **Proxies** → add proxies, health-check them.
3. **Effects** → review FFmpeg presets (applied after 720×1280 crop/scale).
4. **Captions / Hashtags** → seed pools (3–5 tags/post auto-rotated).
5. **Bios** — per-account bio + external bot link (e.g. https://t.me/your_external_bot), rotation interval.
6. **Schedule** → rules (day/hour/minute); beat creates posts every minute with ±5 min jitter.
7. **Videos → Upload** → auto-processes (or trigger manually), then auto-posts at the next due slot.

## Database

Alembic migrations `0001_initial` → … → `0019_post_rule_id` cover the whole
schema. Models live in `backend/app/models/`; secrets (IG passwords, proxy
passwords) are Fernet-encrypted at rest — set a persistent `FERNET_KEY` in
`.env` (changing it later makes stored credentials unreadable).

### Startup schema gate (fail fast, never silent drift)

On boot, the backend compares the revisions in the DB's `alembic_version`
table against the code's alembic heads (`backend/app/core/db_gate.py`). A
fresh database is created from the models and stamped head; an existing one
that is behind/ahead/unversioned **refuses to start** with a
`DatabaseVersionError` naming the exact recovery command. This kills the
recurring incident pattern where new code ran on an unmigrated DB and every
scheduler tick died with `no such column` while nothing alerted.

```bash
# THE migration command — backup first, then upgrade, then verify:
./scripts/migrate.sh
```

`migrate.sh` takes a backup (SQLite snapshot via `backup.sh`, or `pg_dump`
for Postgres), runs `alembic upgrade head` in a **one-off** backend container
(`docker compose run --rm backend …` — works even when the backend is
crash-looping, and always uses the fresh image's migration files), and
aborts unless the DB reports `(head)` afterwards. The upgrade never runs
without a backup: a backup failure stops the script before alembic starts.

Recovery cheat-sheet:

| State | Command |
|---|---|
| Normal deploy with new migration | `./scripts/migrate.sh`, then rebuild/restart |
| Backend refuses to boot: "schema is BEHIND" | `./scripts/migrate.sh` (backs up, then migrates) |
| Backend refuses to boot: "no alembic_version" (legacy `create_all` DB) | verify schema == code, then `docker compose run --rm backend alembic stamp head` |
| Emergency bypass (opts back into silent drift) | `SKIP_DB_VERSION_CHECK=1` — never in normal operation |

### PostgreSQL (optional, recommended for production)

SQLite is the zero-config default (`./data/app.db`). For Postgres:

```bash
# .env
POSTGRES_USER=igfunnel
POSTGRES_PASSWORD=<redacted>   # generate a real one; never commit it
POSTGRES_DB=igfunnel
DATABASE_URL=<redacted>
# SYNC_DATABASE_URL derives automatically (+asyncpg -> +psycopg2)
docker compose --profile postgres up -d
./scripts/migrate.sh            # builds the schema via alembic, with backup
```

`DATABASE_URL` is the async URL (FastAPI, `+asyncpg`); `SYNC_DATABASE_URL` is
the sync URL (Celery, `+psycopg2`) and is derived automatically when left at
its default. All 19 migrations, the models (`str` enums → native PG enums,
timezone-aware datetimes, generic JSON/BigInteger), the SQLite→PG data
migration, and `pg_dump | gzip` backups are tested against a real PostgreSQL
server (16.2; the compose file pins `postgres:15-alpine`, same major-line
behavior for everything used here).

**Moving existing SQLite data to Postgres** (one-time, stack stopped):

```bash
docker compose down   # stop the sqlite stack so the copy sees a quiet DB
docker compose --profile postgres up -d postgres
docker compose run --rm -e DATABASE_URL='postgresql+asyncpg://igfunnel:<pw>@postgres:5432/igfunnel' \
  backend alembic upgrade head
./scripts/backup.sh   # safety snapshot of the sqlite DB
# Run the copy INSIDE a one-off backend container (<pw> = POSTGRES_PASSWORD):
# only there does the hostname `postgres` resolve — the compose file
# publishes no PG port to the host, so @localhost:5432 would fail — and
# /data/app.db is the sqlite file via the ./data:/data mount.
docker compose run --rm -v ./scripts:/scripts \
  -e DATABASE_URL='postgresql+psycopg2://igfunnel:<pw>@postgres:5432/igfunnel' \
  backend python3 /scripts/migrate_sqlite_to_postgres.py /data/app.db
# then point DATABASE_URL at postgres in .env and bring the stack up
```

The script refuses to run into a non-empty target or a schema not at head,
copies every table in FK order inside one transaction (any failure rolls the
whole copy back), localizes naive SQLite timestamps to UTC, resets all `id`
sequences to `MAX(id)`, and re-counts every table afterwards. The source
SQLite file is opened read-only and never modified.

## Backup & restore

The database (`./data/app.db` → `/data/app.db`) and all media (`./media/`
→ `/data/media`, including IG session files under `./media/sessions/`) are
host bind mounts. Back them up daily:

```bash
./scripts/backup.sh            # -> ./data/backups/instareel-YYYYMMDD-HHMMSS.tar.gz
```

The script snapshots SQLite through the online backup API (consistent even
while the worker is writing), tars the snapshot with the whole `./media`
tree, and keeps the last 7 backups (`BACKUP_KEEP=14` to change).
Install as a host cron:

```cron
0 3 * * * /path/to/Instareel/scripts/backup.sh >> /var/log/instareel-backup.log 2>&1
```

Restore:

```bash
docker compose down
mkdir -p /tmp/ir-restore && tar -xzf data/backups/instareel-<stamp>.tar.gz -C /tmp/ir-restore
cp /tmp/ir-restore/app-<stamp>.db data/app.db
cp -a /tmp/ir-restore/media/. media/
docker compose up -d
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Login fails | Check `ADMIN_USERNAME`/`ADMIN_PASSWORD` in `.env` (backend reads them at startup). |
| Video stuck in processing | Check worker logs; FFmpeg stderr is stored in `failed_reason` + SystemLog. |
| `challenge_required` account | Import a fresh session (**Accounts → Upload session**, built via `backend/session_from_browser.py` or `manual_login.py` on a residential IP), then **Test session**. Never password-login from the server IP. |
| Throttled / cooldown | Exponential backoff (6h → 12h → 24h) with automatic rotation to a spare proxy. |
| WS not updating | Token is sent as the first WS message (never in the URL); frontend falls back to polling (30–60s); check Redis is reachable. |
