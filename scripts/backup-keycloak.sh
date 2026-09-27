#!/bin/sh
# =============================================================================
# Keycloak Backup Script
# =============================================================================
# Runs a PostgreSQL dump of the keycloak database and retains the last N backups

BACKUP_DIR="/backups"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
DATE=$(date +%Y-%m-%d_%H-%M-%S)
BACKUP_FILE="${BACKUP_DIR}/keycloak_${DATE}.sql.gz"

echo "[$(date)] Starting Keycloak backup..."

cd "$BACKUP_DIR"

pg_dump --format=custom \
	--compress=9 \
	--host="${PGHOST:-postgres}" \
	--username="${PGUSER:-keycloak}" \
	--dbname="${PGDATABASE:-keycloak}" \
	--file="${BACKUP_FILE}"

if [ $? -eq 0 ]; then
	echo "[$(date)] Backup created: ${BACKUP_FILE}"
else
	echo "[$(date)] ERROR: Backup failed!"
	exit 1
fi

echo "[$(date)] Cleaning up backups older than ${RETENTION_DAYS} days..."
find "$BACKUP_DIR" -name "keycloak_*.sql.gz" -mtime +"$RETENTION_DAYS" -delete

echo "[$(date)] Current backups:"
ls -lh "$BACKUP_DIR"/keycloak_*.sql.gz 2>/dev/null || echo "No backups found"

echo "[$(date)] Backup complete!"
