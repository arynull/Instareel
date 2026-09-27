#!/usr/bin/env bash
# Instareel backup — safe to run while the stack is up.
#
# Layout (host paths, see docker-compose.yml):
#   ./data/app.db   -> /data/app.db        (SQLite)
#   ./media/        -> /data/media          (raw/processed/audio/profile_pics/sessions)
#
# Takes an online-consistent SQLite snapshot (via the sqlite3 backup API,
# so a concurrent writer can't corrupt it), then tars the snapshot together
# with ./media. Keeps the last N backups (default 7) in ./data/backups.
#
# Install as a daily host cron (runs from the compose project directory):
#   0 3 * * * /path/to/Instareel/scripts/backup.sh >> /var/log/instareel-backup.log 2>&1
#
# Restore (see README "Backup & restore" for the full procedure):
#   1. docker compose down
#   2. tar -xzf data/backups/instareel-<stamp>.tar.gz -C /tmp/ir-restore
#   3. cp /tmp/ir-restore/app-<stamp>.db data/app.db
#   4. cp -a /tmp/ir-restore/media/. media/
#   5. docker compose up -d
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

KEEP="${BACKUP_KEEP:-7}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="${BACKUP_DIR:-./data/backups}"
mkdir -p "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)"  # absolute: tar -C args are order-dependent otherwise
SNAP="$BACKUP_DIR/app-$STAMP.db"
TARBALL="$BACKUP_DIR/instareel-$STAMP.tar.gz"

if [ ! -f ./data/app.db ]; then
  echo "backup: ./data/app.db not found — is this the compose project dir?" >&2
  exit 1
fi

echo "backup: snapshotting SQLite (online-consistent)..."
docker compose exec -T backend python3 -c "
import sqlite3
src = sqlite3.connect('/data/app.db', timeout=30)
dst = sqlite3.connect('/data/backups/app-$STAMP.db')
with dst:
    src.backup(dst)
dst.close(); src.close()
print('snapshot ok')
"

echo "backup: archiving (db snapshot + media)..."
# Absolute -C args: each is resolved independently, so 'media' really is
# ./media (which also holds sessions/) and the snapshot really comes from
# ./data/backups — never nested inside itself.
tar -czf "$TARBALL" \
  -C "$BACKUP_DIR" "app-$STAMP.db" \
  -C "$PROJECT_DIR" media
rm -f "$SNAP"

# Sanity: the tarball must contain the db snapshot and the media tree.
# NOTE: don't pipe tar straight into `grep -q` here — with `pipefail`,
# grep's early exit closes the pipe while tar is still writing, tar dies
# with SIGPIPE (141), and the check fails spuriously. Read the listing
# into a variable first.
members="$(tar -tzf "$TARBALL")"
if ! grep -q "app-$STAMP.db" <<< "$members"; then
  echo "backup: ERROR: tarball missing db snapshot" >&2
  exit 1
fi

# Retention: keep the newest $KEEP tarballs.
ls -1t "$BACKUP_DIR"/instareel-*.tar.gz | tail -n +$((KEEP + 1)) | xargs -r rm -f

echo "backup: done -> $TARBALL ($(du -h "$TARBALL" | cut -f1))"
echo "backup: kept: $(ls -1 "$BACKUP_DIR"/instareel-*.tar.gz | wc -l) (KEEP=$KEEP)"
