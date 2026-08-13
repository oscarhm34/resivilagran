#!/bin/bash
# =============================================================================
# Backup script per La Vila Gran - NFC App
# Executa diàriament via Synology Task Scheduler a les 03:00
#
# Què fa:
#   1. pg_dump de PostgreSQL (snapshot consistent)
#   2. Comprimeix uploads, secrets i .env
#   3. Elimina backups > 30 dies
#
# Ús manual: /volume1/docker/NFC2-docker/backup.sh
# =============================================================================

set -e

BACKUP_DIR="/volume1/docker/NFC2-docker/backups"
APP_DIR="/volume1/docker/NFC2-docker"
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/backup_$DATE"
LOG="$BACKUP_DIR/backup.log"

mkdir -p "$BACKUP_DIR"

echo "[$DATE] Inici backup..." >> "$LOG"

# 1. PostgreSQL dump
echo "  Fent pg_dump..." >> "$LOG"
docker exec nfc2-docker-postgres-1 pg_dump -U nfc_app -d cleaning_service \
  --format=custom --compress=6 > "$BACKUP_FILE.pgdump" 2>> "$LOG"

PG_SIZE=$(du -sh "$BACKUP_FILE.pgdump" 2>/dev/null | cut -f1)
echo "  pg_dump OK: $PG_SIZE" >> "$LOG"

# 2. Fitxers: uploads + secrets + .env
echo "  Comprimint fitxers..." >> "$LOG"
tar czf "$BACKUP_FILE.files.tar.gz" \
  -C "$APP_DIR" \
  uploads/ \
  instance/.secret_key \
  instance/.jwt_secret_key \
  instance/.vapid_private_key \
  instance/.vapid_public_key \
  .env 2>/dev/null

FILES_SIZE=$(du -sh "$BACKUP_FILE.files.tar.gz" 2>/dev/null | cut -f1)
echo "  Fitxers OK: $FILES_SIZE" >> "$LOG"

# 3. Neteja: eliminar backups > 30 dies
DELETED=$(find "$BACKUP_DIR" -name "backup_*" -mtime +30 -delete -print | wc -l)
echo "  Neteja: $DELETED fitxers antics eliminats" >> "$LOG"

# 4. Resum
TOTAL_BACKUPS=$(ls "$BACKUP_DIR"/backup_*.pgdump 2>/dev/null | wc -l)
TOTAL_SIZE=$(du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1)
echo "  Backup completat: $BACKUP_FILE (DB: $PG_SIZE, Files: $FILES_SIZE)" >> "$LOG"
echo "  Total backups: $TOTAL_BACKUPS, Espai total: $TOTAL_SIZE" >> "$LOG"
echo "---" >> "$LOG"

echo "Backup completat: $BACKUP_FILE"
