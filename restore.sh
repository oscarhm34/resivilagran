#!/bin/bash
# =============================================================================
# Restore script per La Vila Gran - NFC App
#
# Ús: /volume1/docker/NFC2-docker/restore.sh [nom_backup]
#
# Sense arguments: mostra backups disponibles
# Amb argument:    restaura el backup indicat
#
# Exemple: ./restore.sh backup_20260813_030000
# =============================================================================

BACKUP_DIR="/volume1/docker/NFC2-docker/backups"
APP_DIR="/volume1/docker/NFC2-docker"

# Sense arguments: llistar backups
if [ -z "$1" ]; then
  echo "=== Backups disponibles ==="
  echo ""
  for f in "$BACKUP_DIR"/backup_*.pgdump; do
    [ -f "$f" ] || continue
    NAME=$(basename "$f" .pgdump)
    PG_SIZE=$(du -sh "$f" 2>/dev/null | cut -f1)
    FILES="$BACKUP_DIR/${NAME}.files.tar.gz"
    FILES_SIZE="—"
    [ -f "$FILES" ] && FILES_SIZE=$(du -sh "$FILES" 2>/dev/null | cut -f1)
    # Extreure data del nom
    DATE_PART=$(echo "$NAME" | sed 's/backup_//' | sed 's/_/ /')
    echo "  $NAME  (DB: $PG_SIZE, Fitxers: $FILES_SIZE)  [$DATE_PART]"
  done
  echo ""
  echo "Ús: $0 <nom_backup>"
  echo "Exemple: $0 backup_20260813_030000"
  exit 0
fi

BACKUP_NAME="$1"
PG_FILE="$BACKUP_DIR/${BACKUP_NAME}.pgdump"
FILES_FILE="$BACKUP_DIR/${BACKUP_NAME}.files.tar.gz"

# Verificar que existeix
if [ ! -f "$PG_FILE" ]; then
  echo "ERROR: No existeix $PG_FILE"
  exit 1
fi

echo "=== RESTAURACIÓ ==="
echo "Backup: $BACKUP_NAME"
echo ""
echo "ATENCIÓ: Això sobreescriurà:"
echo "  - Tota la base de dades PostgreSQL"
[ -f "$FILES_FILE" ] && echo "  - Uploads, secrets i .env"
echo ""
read -p "Continuar? (s/N): " confirm
if [ "$confirm" != "s" ] && [ "$confirm" != "S" ]; then
  echo "Cancel·lat."
  exit 0
fi

echo ""

# 1. Restaurar PostgreSQL
echo "[1/3] Restaurant PostgreSQL..."
docker exec -i nfc2-docker-postgres-1 pg_restore -U nfc_app -d cleaning_service \
  --clean --if-exists < "$PG_FILE"
echo "  BD restaurada OK"

# 2. Restaurar fitxers (si existeix)
if [ -f "$FILES_FILE" ]; then
  echo "[2/3] Restaurant fitxers..."
  tar xzf "$FILES_FILE" -C "$APP_DIR"
  echo "  Fitxers restaurats OK"
else
  echo "[2/3] No hi ha backup de fitxers, saltant..."
fi

# 3. Reiniciar app
echo "[3/3] Reiniciant aplicació..."
docker restart nfc2-docker-nfc-1
sleep 5

echo ""
echo "=== Restauració completada ==="
echo "Verifica l'app a: https://lavilagran.synology.me:8444/"
