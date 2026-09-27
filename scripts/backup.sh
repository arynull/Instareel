#!/usr/bin/env bash
# Instareel /data backup — safe to run while the stack is up.
#
# Takes an online-consistent SQLite snapshot (via the sqlite3 backup API,
# so a concurrent writer can't corrupt it), then tars the snapshot together
# with media/ and sessions/. Keeps the last N backups (default 7).
#
# Install as a daily host cron (runs from the compose project directory):
#   0 3 * * * /path/to/Instareel/scripts/backup.sh >> /var/log/instareel-backup.log 2>&1
#
# Restore: stop the stack, extract the tarball over ./data, then start.
set -euo pipefail

cd "$(dirname "$0")/.."

KEEP="${BACKUP_KEEP:-7}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="${BACKUP_DIR:-./data/backups}"
mkdir -p "$BACKUP_DIR"
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

echo "backup: archiving..."
tar -czf "$TARBALL" \
  -C "$BACKUP_DIR" "app-$STAMP.db" \
  -C ./data media sessions 2>/dev/null || \
tar -czf "$TARBALL" -C "$BACKUP_DIR" "app-$STAMP.db"
rm -f "$SNAP"

# Retention: keep the newest $KEEP tarballs.
ls -1t "$BACKUP_DIR"/instareel-*.tar.gz | tail -n +$((KEEP + 1)) | xargs -r rm -f

echo "backup: done -> $TARBALL ($(du -h "$TARBALL" | cut -f1))"
echo "backup: kept: $(ls -1 "$BACKUP_DIR"/instareel-*.tar.gz | wc -l) (KEEP=$KEEP)"
