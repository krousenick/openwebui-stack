#!/bin/sh
# =============================================================================
# Keycloak Backup Script
# =============================================================================
# Runs a PostgreSQL dump of the keycloak database and retains the last N backups

BACKUP_DIR="/backups"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
DATE=$(date +%Y-%m-%d_%H-%M-%S)
# pg_dump --format=custom produces a single compressed archive; the
# conventional extension is .dump (.sql.gz would imply plain SQL gzip).
BACKUP_FILE="${BACKUP_DIR}/keycloak_${DATE}.dump"

echo "[$(date)] Starting Keycloak backup..."

cd "$BACKUP_DIR" || exit 1

if pg_dump --format=custom \
    --compress=9 \
    --host="${PGHOST:-postgres}" \
    --username="${PGUSER:-keycloak}" \
    --dbname="${PGDATABASE:-keycloak}" \
    --file="${BACKUP_FILE}"; then
    echo "[$(date)] Backup created: ${BACKUP_FILE}"
    echo "keycloak_backup_success 1"
else
    echo "[$(date)] ERROR: Backup failed!"
    echo "keycloak_backup_success 0"
    exit 1
fi

echo "[$(date)] Cleaning up backups older than ${RETENTION_DAYS} days..."
find "$BACKUP_DIR" -name "keycloak_*.dump" -mtime +"$RETENTION_DAYS" -delete

echo "[$(date)] Current backups:"
ls -lh "$BACKUP_DIR"/keycloak_*.dump 2>/dev/null || echo "No backups found"

echo "[$(date)] Backup complete!"
