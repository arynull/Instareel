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
| postgres | Optional — enable with `DATABASE_URL=postgresql+asyncpg://…` and `--profile postgres` |

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
alembic upgrade head          # REQUIRED on existing DBs — startup only creates missing *tables*, never new *columns*
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

Alembic migrations `0001_initial` → … → `0018_post_dispatched_at` cover the whole
schema — always run `alembic upgrade head` after pulling.
Models live in `backend/app/models/`; secrets (IG passwords, proxy passwords)
are Fernet-encrypted at rest — set a persistent `FERNET_KEY` in `.env`
(changing it later makes stored credentials unreadable).

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
