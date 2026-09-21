#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${JBB_PROJECT_DIR:-/opt/jubianbian}"
BACKUP_DIR="${JBB_BACKUP_DIR:-/opt/jubianbian-backups}"
RETENTION_DAYS="${JBB_BACKUP_RETENTION_DAYS:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="${BACKUP_DIR}/jbb-data-${STAMP}.tar.gz"
TMP="${ARCHIVE}.tmp"

mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"
cd "${PROJECT_DIR}"

# The named Docker volume contains SQLite and uploaded videos. Stream the
# archive out of the app container so no host-side volume path is required.
docker compose exec -T app tar -czf - -C /app/data . > "${TMP}"
mv "${TMP}" "${ARCHIVE}"
find "${BACKUP_DIR}" -maxdepth 1 -type f -name 'jbb-data-*.tar.gz' -mtime "+${RETENTION_DAYS}" -delete

echo "backup=${ARCHIVE}"
