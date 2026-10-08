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
#
# L'ordre importa: primer es comprova que la còpia serveix, després es fa una
# còpia de seguretat de l'estat actual, i només llavors es toca res. Abans es
# feia al revés i un dump corrupte s'enduia per davant la base de dades viva.
#
# IMPORTANT: aquest fitxer NO el copia el procés de desplegament. Quan canvia,
# cal pujar-lo a mà al NAS.
# =============================================================================

set -euo pipefail

BACKUP_DIR="/volume1/docker/NFC2-docker/backups"
APP_DIR="/volume1/docker/NFC2-docker"
PASS_FILE="${BACKUP_PASS_FILE:-/volume1/docker/.backup_pass}"

# Sense arguments: llistar backups
if [ -z "${1:-}" ]; then
  echo "=== Backups disponibles ==="
  echo ""
  for f in "$BACKUP_DIR"/backup_*.pgdump; do
    [ -f "$f" ] || continue
    NAME=$(basename "$f" .pgdump)
    PG_SIZE=$(du -sh "$f" 2>/dev/null | cut -f1 || echo "?")
    FILES="$BACKUP_DIR/${NAME}.files.tar.gz"
    FILES_SIZE="—"
    [ -f "$FILES" ] && FILES_SIZE=$(du -sh "$FILES" 2>/dev/null | cut -f1 || echo "?")
    SECRETS="—"
    [ -f "$BACKUP_DIR/${NAME}.secrets.tar.gz.gpg" ] && SECRETS="xifrats"
    [ -f "$BACKUP_DIR/${NAME}.secrets.tar.gz" ] && SECRETS="SENSE XIFRAR"
    DATE_PART=$(echo "$NAME" | sed 's/backup_//' | sed 's/_/ /')
    echo "  $NAME  (DB: $PG_SIZE, Fitxers: $FILES_SIZE, Secrets: $SECRETS)  [$DATE_PART]"
  done
  echo ""
  echo "Ús: $0 <nom_backup>"
  echo "Exemple: $0 backup_20260813_030000"
  exit 0
fi

BACKUP_NAME="$1"
PG_FILE="$BACKUP_DIR/${BACKUP_NAME}.pgdump"
FILES_FILE="$BACKUP_DIR/${BACKUP_NAME}.files.tar.gz"
SECRETS_GPG="$BACKUP_DIR/${BACKUP_NAME}.secrets.tar.gz.gpg"
SECRETS_PLA="$BACKUP_DIR/${BACKUP_NAME}.secrets.tar.gz"

# ── 0. Comprovar que la còpia serveix, ABANS de tocar res ───────────────────

if [ ! -f "$PG_FILE" ]; then
  echo "ERROR: No existeix $PG_FILE"
  exit 1
fi

echo "[0/4] Verificant la còpia abans de tocar res..."

# Si pg_restore no pot llegir l'índex, el fitxer està truncat o corrupte. Val
# més descobrir-ho ara que després d'haver esborrat l'esquema.
if ! docker exec -i nfc2-docker-postgres-1 pg_restore --list < "$PG_FILE" > /dev/null 2>&1; then
  echo "ERROR: el dump no es pot llegir. NO s'ha tocat res."
  echo "       Prova amb una còpia anterior: $0"
  exit 1
fi
echo "  Dump de la base de dades: correcte"

if [ -f "$FILES_FILE" ]; then
  if ! tar tzf "$FILES_FILE" > /dev/null 2>&1; then
    echo "ERROR: el tar de fitxers està corrupte. NO s'ha tocat res."
    exit 1
  fi
  echo "  Arxiu de fitxers: correcte"
fi

if [ -f "$SECRETS_GPG" ] && [ ! -s "$PASS_FILE" ]; then
  echo "ERROR: els secrets estan xifrats i no hi ha $PASS_FILE per desxifrar-los."
  echo "       Sense ells l'aplicació no arrencarà. NO s'ha tocat res."
  exit 1
fi

echo ""
echo "=== RESTAURACIÓ ==="
echo "Backup: $BACKUP_NAME"
echo ""
echo "ATENCIÓ: Això sobreescriurà:"
echo "  - Tota la base de dades PostgreSQL"
[ -f "$FILES_FILE" ] && echo "  - Els fitxers pujats (uploads)"
{ [ -f "$SECRETS_GPG" ] || [ -f "$SECRETS_PLA" ]; } && echo "  - Els secrets i el .env"
echo ""
read -p "Continuar? (s/N): " confirm
if [ "$confirm" != "s" ] && [ "$confirm" != "S" ]; then
  echo "Cancel·lat."
  exit 0
fi

echo ""

# ── 1. Xarxa de seguretat: desar l'estat actual ─────────────────────────────
# Per al cas "he restaurat la còpia equivocada", que passa més del que sembla
# quan es treballa amb pressa i de matinada.
SALVAVIDES="$BACKUP_DIR/pre_restore_$(date +%Y%m%d_%H%M%S).pgdump"
echo "[1/4] Desant l'estat actual a $(basename "$SALVAVIDES")..."
if docker exec nfc2-docker-postgres-1 pg_dump -U nfc_app -d cleaning_service \
     --format=custom --compress=6 > "$SALVAVIDES" 2>/dev/null; then
  echo "  Desat"
else
  rm -f "$SALVAVIDES"
  echo "  AVIS: no s'ha pogut desar l'estat actual."
  read -p "  Continuar igualment? (s/N): " seguir
  [ "$seguir" = "s" ] || [ "$seguir" = "S" ] || { echo "Cancel·lat."; exit 0; }
fi

# ── 2. Restaurar PostgreSQL ─────────────────────────────────────────────────
echo "[2/4] Restaurant PostgreSQL..."
# pg_restore retorna != 0 per avisos benignes (objectes que no existien al fer
# el --clean), així que no es pot tractar el codi de sortida com un error sec.
if docker exec -i nfc2-docker-postgres-1 pg_restore -U nfc_app -d cleaning_service \
     --clean --if-exists < "$PG_FILE"; then
  echo "  BD restaurada OK"
else
  echo "  BD restaurada amb avisos (normal amb --clean). Revisa la sortida."
fi

# ── 3. Restaurar fitxers i secrets ──────────────────────────────────────────
echo "[3/4] Restaurant fitxers..."
if [ -f "$FILES_FILE" ]; then
  tar xzf "$FILES_FILE" -C "$APP_DIR"
  echo "  Fitxers restaurats OK"
else
  echo "  No hi ha arxiu de fitxers, saltant..."
fi

# Les còpies anteriors al canvi porten els secrets dins del tar de fitxers;
# les noves els porten a part i xifrats. Es contemplen les dues.
if [ -f "$SECRETS_GPG" ]; then
  gpg --decrypt --batch --quiet --passphrase-file "$PASS_FILE" \
      "$SECRETS_GPG" 2>/dev/null | tar xzf - -C "$APP_DIR"
  echo "  Secrets desxifrats i restaurats OK"
elif [ -f "$SECRETS_PLA" ]; then
  tar xzf "$SECRETS_PLA" -C "$APP_DIR"
  echo "  Secrets restaurats OK (no estaven xifrats)"
else
  echo "  Sense arxiu de secrets propi (còpia antiga: anaven dins dels fitxers)"
fi

# ── 4. Reiniciar i comprovar que tot plegat ha servit ───────────────────────
echo "[4/4] Reiniciant aplicació..."
docker restart nfc2-docker-nfc-1 > /dev/null
sleep 8

# Una restauració sense comprovació és una esperança. Dues preguntes senzilles:
# hi ha dades? i l'aplicació contesta?
TREBALLADORES=$(docker exec nfc2-docker-postgres-1 psql -U nfc_app -d cleaning_service \
  -tAc "SELECT count(*) FROM cleaner" 2>/dev/null || echo "?")
echo "  Treballadores a la base de dades: $TREBALLADORES"

if curl -fsS -o /dev/null --max-time 15 http://localhost:8088/admin/login 2>/dev/null; then
  echo "  L'aplicació contesta"
else
  echo "  AVIS: l'aplicació encara no contesta. Mira: docker logs nfc2-docker-nfc-1 --tail 50"
fi

echo ""
echo "=== Restauració completada ==="
echo "L'estat anterior ha quedat desat a: $(basename "$SALVAVIDES")"
