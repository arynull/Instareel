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
| worker   | Celery worker (video queue + posts queue) |
| beat     | Celery Beat (per-minute scheduler, analytics 4h, bio rotation, proxy checks, cleanup) |
| redis    | Broker + result backend + realtime pub/sub + progress keys |
| frontend | Next.js 14 dashboard |
| postgres | Optional — enable with `DATABASE_URL=postgresql+asyncpg://…` and `--profile postgres` |

Default is SQLite (`./data/app.db` bind-mounted from the repo root) — zero-config.

## Local dev (no Docker)

Backend needs Python 3.11+, FFmpeg, and Redis:

```bash
cd backend
pip install -r requirements.txt
cp ../.env.example ../.env   # or set env vars
uvicorn app.main:app --reload
celery -A app.tasks.celery_app.celery worker --loglevel=info
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

Alembic migrations `0001_initial` → `0002_audio_tracks` → `0003_bio_profile`
cover the whole schema — always run `alembic upgrade head` after pulling.
Models live in `backend/app/models/`; secrets (IG passwords, proxy passwords)
are Fernet-encrypted at rest — set a persistent `FERNET_KEY` in `.env`
(changing it later makes stored credentials unreadable).

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Login fails | Check `ADMIN_USERNAME`/`ADMIN_PASSWORD` in `.env` (backend reads them at startup). |
| Video stuck in processing | Check worker logs; FFmpeg stderr is stored in `failed_reason` + SystemLog. |
| `challenge_required` account | Import a fresh session (**Accounts → Upload session**, built via `backend/session_from_browser.py` or `manual_login.py` on a residential IP), then **Test session**. Never password-login from the server IP. |
| Throttled / cooldown | Exponential backoff (6h → 12h → 24h) with automatic rotation to a spare proxy. |
| WS not updating | Token is sent as the first WS message (never in the URL); frontend falls back to polling (30–60s); check Redis is reachable. |
