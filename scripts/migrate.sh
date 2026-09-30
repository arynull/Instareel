#!/usr/bin/env bash
# Instareel database migration — NEVER upgrades without a fresh backup.
#
#   ./scripts/migrate.sh              # backup + `alembic upgrade head` + verify
#   ./scripts/migrate.sh --backup-only
#
# Why this exists: migrations 0018/0019 once ran (or silently no-op'd) in the
# wrong container and the new code died with "no such column" on every
# scheduler tick. Two guardrails now prevent a repeat:
#   1. This script refuses to upgrade without taking a fresh backup first.
#   2. The backend's startup gate (backend/app/core/db_gate.py) refuses to
#      boot when the DB schema is behind the code, with the recovery command.
#
# The upgrade itself runs in a ONE-OFF backend container
# (`docker compose run --rm backend ...`), not `exec` into the running one:
# it works even when the backend is down or crash-looping on a schema
# mismatch, and it always uses the freshly-built image's migration files.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

BACKUP_ONLY=0
case "${1:-}" in
  "") ;;
  --backup-only) BACKUP_ONLY=1 ;;
  *)
    echo "migrate: ERROR: unknown argument '$1' (usage: ./scripts/migrate.sh [--backup-only])" >&2
    exit 2
    ;;
esac

# --- Detect the database dialect from .env (no python dependency) ---
_env_val() { # _env_val KEY DEFAULT — last matching line wins, quotes stripped
  local v
  v="$(grep -E "^[[:space:]]*$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d ' "\"' || true)"
  echo "${v:-$2}"
}
DB_URL="$(_env_val DATABASE_URL "sqlite+aiosqlite:///./data/app.db")"

BACKUP_DIR="./data/backups"
mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"

pg_env() {
  # Read the three PG vars with grep — deliberately NOT `source .env`, which
  # would export every secret into this shell (and into compose interpolation).
  PGUSER="$(_env_val POSTGRES_USER igfunnel)"
  PGDB="$(_env_val POSTGRES_DB igfunnel)"
  PGPASSWORD="$(_env_val POSTGRES_PASSWORD "")"
}

if [[ "$DB_URL" == *"postgres"* ]]; then
  echo "migrate: dialect=postgres — dumping before upgrade..."
  pg_env
  docker compose --profile postgres up -d postgres >/dev/null
  DUMP="$BACKUP_DIR/pg-$STAMP.sql.gz"
  TMP_DUMP="$DUMP.tmp"
  # PGPASSWORD is passed defensively: local socket auth is usually trust, but
  # a password-configured server would otherwise fail the dump and — because
  # of set -euo pipefail — correctly abort the migration.
  # The dump goes to a temp file first: a failed pg_dump must never leave a
  # truncated .sql.gz behind that looks like a valid backup.
  docker compose exec -T -e PGPASSWORD="$PGPASSWORD" postgres \
    pg_dump -U "$PGUSER" "$PGDB" | gzip > "$TMP_DUMP"
  mv "$TMP_DUMP" "$DUMP"
  echo "migrate: pg_dump -> $DUMP ($(du -h "$DUMP" | cut -f1))"
  # Retention (same KEEP semantics as backup.sh).
  KEEP="${BACKUP_KEEP:-7}"
  ls -1t "$BACKUP_DIR"/pg-*.sql.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
else
  echo "migrate: dialect=sqlite — snapshotting before upgrade..."
  ./scripts/backup.sh
fi

if [[ "$BACKUP_ONLY" -eq 1 ]]; then
  echo "migrate: --backup-only, stopping before upgrade."
  exit 0
fi

echo "migrate: running alembic upgrade head (one-off container)..."
docker compose run --rm backend alembic upgrade head

echo "migrate: verifying..."
CURRENT="$(docker compose run --rm backend alembic current)"
echo "migrate: $CURRENT"
if [[ "$CURRENT" != *"(head)"* ]]; then
  echo "migrate: ERROR: database is not at head after upgrade — investigate before restarting." >&2
  exit 1
fi
echo "migrate: OK — database at head. Safe to (re)start the stack."
