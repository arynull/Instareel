# IG Funnel — Instagram Reels automation

Admin uploads videos via the dashboard. The backend processes them with
FFmpeg, posts them to Instagram Reels on schedule (Celery Beat + two worker
lanes). All configuration lives in the dashboard UI. The Instagram bio can
link to an external bot (e.g. a Telegram bot) via a plain URL field — there
is no Telegram integration in this project.

## Architecture

```
                    ┌──────────── nginx :8080 (only ingress) ────────────┐
                    │  /  → frontend      /api/* /ws /docs → backend      │
                    └──────────────┬──────────────────────┬───────────────┘
                                   │                      │
                    ┌──────────────▼───────┐   ┌──────────▼────────┐
                    │ frontend (Next.js 14)│   │ backend (FastAPI) │
                    │  same-origin /api    │   │  + /ws realtime   │
                    └──────────────────────┘   └──────┬────────────┘
                                                     │  ┌───────┴────────┐
                    ┌──────────────┐   ┌──────────────▼──▼───┐  ┌─────────▼────────┐
                    │ beat         │   │ redis (broker,      │  │ worker-fast (Q:  │
                    │ (scheduler)  │──▶│ pub/sub, locks,     │  │ fast)  scheduler │
                    └──────────────┘   │ progress keys)     │  │ ticks, watchdog  │
                                       └─────────┬──────────┘  └──────────────────┘
                                                 │             ┌──────────────────┐
                                                 └────────────▶│ worker-slow (Q:  │
                                                               │ slow)  FFmpeg,   │
                                                               │ uploads, ingest, │
                                                               │ analytics        │
                                                               └──────────────────┘
```

State: SQLite (`./data/app.db`) by default, optional PostgreSQL
(`--profile postgres`). Media + IG session files live under `./media/`
(bind-mounted to `/data/media` in containers).

## Quick start (Docker)

```bash
cp .env.example .env
# edit .env: SECRET_KEY, FERNET_KEY, ADMIN_USERNAME, ADMIN_PASSWORD, DATABASE_URL…
docker compose up --build
```

- Default deploy includes nginx (the only ingress): everything on
  `${NGINX_PORT:-8080}` — `/` → frontend, `/api/*` + `/ws` + docs → backend,
  same-origin (no CORS). The frontend image is baked for this: the
  server-side `/api/*` rewrite points at `API_INTERNAL_URL`
  (`http://backend:8000` build arg) and `NEXT_PUBLIC_API_URL` stays empty.
  `CLIENT_MAX_BODY_SIZE` must stay above the backend's `MAX_UPLOAD_MB`
  (defaults: 550m vs 500) or nginx 413s uploads.
- Only set `FRONTEND_API_URL` when exposing the frontend container directly
  (no nginx) — it is baked into the client bundle at build time.
- API docs (`/docs`, `/openapi.json`) are disabled by default for security —
  set `DOCS_ENABLED=true` to expose them (local dev only).
- Log in with `ADMIN_USERNAME` / `ADMIN_PASSWORD` from `.env`.
- Run `./scripts/migrate.sh` (not bare `alembic upgrade head`) whenever a
  deploy includes a new migration — see "Database" below.

## Services

| Service     | Role |
|-------------|------|
| backend     | FastAPI + Uvicorn (port 8000), REST API + `/ws` realtime feed |
| worker-fast | Celery worker, **fast** lane: scheduler ticks, watchdog, heartbeats, proxy checks |
| worker-slow | Celery worker, **slow** lane: FFmpeg processing, uploads, source ingest, analytics |
| beat        | Celery Beat — the 10 periodic tasks listed below |
| redis       | Broker + result backend + realtime pub/sub + progress keys + locks |
| frontend    | Next.js 14 dashboard (standalone build, served on :3000 behind nginx) |
| postgres    | Optional (`--profile postgres`) — see "Database → PostgreSQL" |

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

**Never recreate `worker-slow` while a post is `posting` or near a schedule
slot** — with a single slow worker no in-flight upload survives a container
recreation (a mid-upload deploy once left a post stuck on `posting` 31+
minutes). Boot-time and periodic reapers fail such orphans closed (video
quarantined + critical notification), but the upload itself is lost.

### Beat schedule (all in `SCHEDULE_TZ`, default `Asia/Tehran`)

| Every | Task | What it does |
|-------|------|--------------|
| 1 min | `check_and_post` | Fires due schedule-rule slots (grace window + jitter), dispatches to slow lane |
| 1 min | `beat_heartbeat` | Liveness proof for the Health page |
| 2 min | `system_watchdog` | Component state changes → down/recovered notifications; prunes old notifications |
| 10 min | `reap_stale_posting` | Fails posts wedged in `posting` (worker died mid-upload) |
| 30 min | `check_all_proxies` | Proxy health checks |
| 3 h (:17) | `refresh_proxy_pool` | Proxy pool refresh |
| 4 h | `fetch_all_analytics` | Reels view/like counts sweep (slow lane). Reads `play_count` (live reel metric) and prefers `ig_play_count` (the app's unified views metric) when present. An all-zero result on a post under 48 h old is treated as "not indexed yet" and retried on the next sweep instead of freezing a zero in the panel |
| 6 h (:23) | `scan_shadowban` | View-collapse heuristic per account (fast lane, DB scan only) |
| daily 00:00 | `reset_daily_counts` | Per-account daily post counters |
| daily 04:00 | `clean_old_media` | Old media cleanup |

### Anti-detection & reliability

- **Human-like posting jitter**: each fired slot gets a random 0–N minute
  offset (setting `post_jitter_minutes`, default 5) with second resolution —
  the post is dispatched to the slow lane via `apply_async(countdown=…)`
  at the exact second, not rounded to the next minute tick.
- **Schedule grace window**: a rule slot stays fireable `SCHEDULE_GRACE_MINUTES`
  (default 5, env) after its minute, so a brief worker/beat outage posts late
  instead of silently losing the slot. `Post.slot_for` + per-rule dedup give
  exactly-once firing per slot (a DB unique constraint backstops tick races).
- **Exactly-once dispatch**: every post created by the tick is stamped
  `dispatched_at`; the per-minute backstop atomically
  (`UPDATE … WHERE … RETURNING`) picks up only unstamped posts (manual/API
  schedules) or stamps older than `dispatch_stale_minutes` (default 30 —
  re-dispatched, never lost). The atomic `claim_post` guarantees no
  double-upload even if two tasks ever race.
- **New-account warm-up** (`warmup_days`, default 7): accounts younger than N
  days post at most 1/day. Set to 0 for old accounts newly connected here.
- **Action-block detection**: `feedback_required` from Instagram → account
  goes to `cooldown` for `action_block_cooldown_hours` (default 24), proxy
  rotates to a proven spare if one exists, and a dedicated warning
  notification fires (deduped daily). No pointless retries against a block.
  Nothing ever password-logs-in automatically — a dead session parks the post
  and notifies; only the explicit **Login** button (async, 202 + status poll)
  performs a login.
- **Shadowban scan** (every 6h, `shadowban_scan_enabled`): compares the
  median 24h views of recent posts against the median 7d views of the
  account's baseline — a collapse below `shadowban_collapse_ratio`
  (default 0.10) pauses the account for `shadowban_pause_hours` (default
  48) and notifies; recovery auto-resumes only shadowban episodes, never
  manual pauses.

## Dashboard pages (`/dashboard/*`)

| Page | What it's for |
|------|---------------|
| Overview (`/`) | Command center: production funnel, 7×24 best-hours heatmap, live status bar, sparklines |
| Posts | Post pipeline table (status, rule, checked-relative-time), manual scheduling |
| Videos | Library, upload (auto-process), per-video detail + live effect preview |
| Schedule | Schedule rules (day/hour/minute, per-account, pinned video, one-shot) |
| Accounts | IG accounts, async login flow, session test/upload, live stats |
| Sources | Anonymous-only source ingest (web_profile_info + yt-dlp, spare-proxy pulls) |
| Analytics | Views/likes trends, per-account comparison, engagement heatmap |
| Proxies | Pool management, health checks, purge/refresh |
| Effects / Audio / Captions | FFmpeg effect presets, trending audio tracks, caption/hashtag pools |
| Bios | Per-account bio config (text, link URL, name, picture, privacy) — one account = one config, applied on demand |
| Phone | Phone-frame preview of the live Instagram profile |
| Health | Component checks (DB, Redis, workers, beat, sessions, proxies, FFmpeg, disk) |
| Server | Live server stats |
| Logs | System logs with filters |
| Settings | All tunable settings (see below) |
| Notifications | Bell: unread badge, upcoming-post countdowns, recent events |

### Realtime

The backend publishes events (`new_log`, `notification`, `analytics_update`,
`schedule_update`) over Redis pub/sub; the dashboard consumes them on `/ws`
— the token is sent as the **first WS message** (`{"token": ...}`), never in
the URL. On reconnect the frontend refetches all realtime keys; a 5-minute
safety poll remains as backstop (server-stats/health/video-progress pages
poll intentionally). All publishing is best-effort: failures are swallowed so
a down Redis never breaks API responses.

## Local dev (no Docker)

Backend needs Python 3.11+, FFmpeg, and Redis:

```bash
cd backend
pip install -r requirements.txt
cp ../.env.example ../.env   # or set env vars
uvicorn app.main:app --reload
# Two lane workers (see "Task lanes" above): the fast lane runs scheduler
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
   Import a session built on a residential IP (**Upload session**, via
   `backend/session_from_browser.py` or `backend/manual_login.py`), then
   **Test session**. Never password-login from the server IP.
2. **Proxies** → add proxies, health-check them.
3. **Effects** → review FFmpeg presets (applied after 720×1280 crop/scale).
4. **Captions / Hashtags** → seed pools (3–5 tags/post auto-rotated).
5. **Bios** — per-account bio + external link (e.g. `https://t.me/your_bot`),
   applied on demand from the Bios page.
6. **Schedule** → rules (day/hour/minute); beat fires slots every minute with
   grace window + jitter.
7. **Videos → Upload** → auto-processes (or trigger manually), then auto-posts
   at the next due slot.

## Configuration reference

### Environment (`.env` — see `.env.example`)

| Variable | Purpose |
|----------|---------|
| `SECRET_KEY` / `FERNET_KEY` | JWT signing / credential encryption at rest. `FERNET_KEY` must be persistent — changing it makes stored IG/proxy passwords unreadable |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | Dashboard login (read at backend startup) |
| `DATABASE_URL` | Async DB URL. **Inside containers** must use the absolute SQLite path `sqlite+aiosqlite:////data/app.db` or a `postgresql+asyncpg://` URL — never a relative SQLite path, never `localhost` for redis/postgres hostnames |
| `SYNC_DATABASE_URL` | Sync DB URL for Celery (derives automatically: `+asyncpg`→`+psycopg2`) |
| `REDIS_URL` | `redis://redis:6379/0` (compose service name, not localhost) |
| `SCHEDULE_TZ` | IANA timezone for rule slots (e.g. `Asia/Tehran`) |
| `SCHEDULE_GRACE_MINUTES` | Slot grace window (default 5, 0 = legacy exact-minute) |
| `MAX_UPLOAD_MB` | Upload cap (default 500; keep below nginx `CLIENT_MAX_BODY_SIZE`) |
| `DOCS_ENABLED` | Expose `/docs` + `/openapi.json` (default false) |
| `IG_DEFAULT_MAX_DAILY_POSTS`, `IG_PRE_POST_DELAY_MIN/MAX` | Posting guardrails |

Hard-won rules: never a relative SQLite `DATABASE_URL` inside containers
(startup rejects it); never `redis://localhost` inside containers; never set
`FRONTEND_API_URL` when nginx is the ingress (it gets baked into the JS
bundle and breaks same-origin calls).

### Tunable settings (dashboard **Settings** page / `PUT /api/v1/settings/{key}`)

| Key | Default | Meaning |
|-----|---------|---------|
| `post_jitter_minutes` | 5 | Random post delay per slot (minutes) |
| `dispatch_stale_minutes` | 30 | Re-dispatch posts whose dispatch stamp is older |
| `warmup_days` | 7 | New-account 1-post/day warm-up (0 = disabled) |
| `shadowban_scan_enabled` | true | 6-hourly view-collapse scan |
| `shadowban_pause_hours` | 48 | Auto-pause duration on view collapse |
| `shadowban_collapse_ratio` | 0.10 | Collapse threshold vs baseline |
| `shadowban_min_baseline_views` / `shadowban_min_recent_posts` | 100 / 3 | Scan sensitivity floors |
| `action_block_cooldown_hours` | 24 | Cooldown after `feedback_required` |
| `auto_process_on_upload` | true | FFmpeg-process right after upload |
| `pool_country` / `pool_require_country` / `pool_purge_after_days` / `pool_stillborn_hours` | — | Proxy pool policy |

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
**Build the backend image before migrating** so the one-off container sees
the current code's migration files.

Recovery cheat-sheet:

| State | Command |
|---|---|
| Normal deploy with new migration | `git pull`, `docker compose build backend`, `./scripts/migrate.sh`, then rebuild/restart |
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

## CI & tests

GitHub Actions (`.github/workflows/ci.yml`) runs on every push/PR to `main`:

- **backend**: Python 3.12, `pip install -r backend/requirements.txt`, full
  `pytest` suite (hermetic: in-memory SQLite + FakeRedis; the CI installs
  `ffmpeg`/`ffprobe` for the media e2e tests).
- **frontend**: Node 20, `npm ci`, `tsc --noEmit`, `vitest run`,
  production `next build`.

Run locally:

```bash
cd backend && python -m pytest tests/ -q        # full backend suite
cd frontend && npx tsc --noEmit && npx vitest run && npm run build
```

## API overview

All REST endpoints live under `/api/v1` (`backend/app/api/`); the realtime
feed is at `/ws` on the app root.

| Prefix | Module | Resources |
|--------|--------|-----------|
| `/auth` | `api/auth.py` | Login, token refresh |
| `/accounts` | `api/accounts.py` | IG accounts, async login flow, session test/upload |
| `/videos`, `/posts` | `api/videos.py` | Video library, upload/process, post pipeline |
| `/schedule` | `api/scheduling.py` | Schedule rules |
| `/sources` | `api/sources.py` | Anonymous source ingest |
| `/captions`, `/hashtags` | `api/scheduling.py` | Caption templates, hashtag sets |
| `/bios` | `api/resources.py` | Bio configs |
| `/proxies` | `api/resources.py` | Proxy pool |
| `/effects`, `/audio` | `api/resources.py` | FFmpeg presets, audio tracks |
| `/analytics`, `/logs`, `/settings`, `/system` | `api/system.py` | Stats, logs, settings, health |
| `/notifications` | `api/notifications.py` | Notification center |

## Project structure

```
Instareel/
├── backend/
│   ├── app/
│   │   ├── api/          # FastAPI routers (auth, accounts, videos, scheduling, …)
│   │   ├── core/         # db_gate (startup schema check), security, logging
│   │   ├── models/       # SQLAlchemy models
│   │   ├── schemas/      # Pydantic request/response schemas
│   │   ├── services/     # scheduler, instagram, video_processor, proxies, realtime…
│   │   ├── tasks/        # Celery tasks (post, video, health, proxy, analytics…)
│   │   ├── utils/        # ffmpeg helpers, crypto, misc
│   │   ├── alembic/      # migrations 0001 → 0019
│   │   ├── config.py     # env-based settings (+ validation)
│   │   ├── database.py   # async + sync engines, SQLite pragmas (FK/WAL)
│   │   └── main.py       # app factory, lifespan, router mounting
│   ├── tests/            # hermetic pytest suite
│   ├── manual_login.py / session_from_browser.py  # session builders (residential IP!)
│   └── requirements.txt
├── frontend/src/
│   ├── app/              # Next.js routes (dashboard/*, login)
│   ├── components/       # UI components
│   ├── hooks/ lib/ stores/  # react-query hooks, api client, zustand stores
├── nginx/                # reverse proxy config (only ingress)
├── scripts/              # backup.sh, migrate.sh, migrate_sqlite_to_postgres.py
├── docker-compose.yml
└── .env.example
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Login fails | Check `ADMIN_USERNAME`/`ADMIN_PASSWORD` in `.env` (backend reads them at startup). |
| Backend refuses to boot (schema BEHIND/AHEAD) | Run `./scripts/migrate.sh` — see "Database" |
| Video stuck in processing | Check worker-slow logs; FFmpeg stderr is stored in `failed_reason` + SystemLog. |
| Post stuck in `posting` | `reap_stale_posting` (every 10 min) fails it closed; check worker-slow wasn't recreated mid-upload. |
| `challenge_required` / dead session | Import a fresh session (**Accounts → Upload session**, built via `backend/session_from_browser.py` or `manual_login.py` on a residential IP), then **Test session**. Never password-login from the server IP, and never let code auto-login (only the Login button does). |
| Throttled / cooldown | Automatic: backoff with rotation to a spare proxy; check **Proxies**. |
| False "Scheduler was down" | Expected once after a worker recreate (amber Degraded); the watchdog distinguishes genuinely-down from busy-posting via per-lane alive keys. |
| WS not updating | Token is sent as the first WS message (never in the URL); frontend falls back to polling (5-min backstop); check Redis is reachable. |
| Deleting a rule 500'd (fixed) | Old versions: FK on `posts.rule_id`. Fixed — delete nullifies the link, posts keep history. |
