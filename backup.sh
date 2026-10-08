#!/bin/bash
# =============================================================================
# Backup script per La Vila Gran - NFC App
# Executa diàriament via Synology Task Scheduler a les 03:00
#
# Què fa:
#   1. pg_dump de PostgreSQL i EL VERIFICA abans de donar-lo per bo
#   2. Comprimeix uploads (sense secrets)
#   3. Desa els secrets a part i xifrats
#   4. Elimina backups > 30 dies
#
# Ús manual: /volume1/docker/NFC2-docker/backup.sh
#
# IMPORTANT: aquest fitxer NO el copia el procés de desplegament. Quan canvia,
# cal pujar-lo a mà al NAS.
# =============================================================================

set -e

BACKUP_DIR="/volume1/docker/NFC2-docker/backups"
APP_DIR="/volume1/docker/NFC2-docker"
# El fitxer amb la contrasenya del xifrat. Fora de $BACKUP_DIR a propòsit: si
# estigués dins, aniria dins de la còpia que protegeix.
PASS_FILE="${BACKUP_PASS_FILE:-/volume1/docker/.backup_pass}"
MIDA_MINIMA_DUMP=100000          # Menys de 100 KB no és una base de dades real
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/backup_$DATE"
LOG="$BACKUP_DIR/backup.log"

mkdir -p "$BACKUP_DIR"

# Qualsevol sortida per error deixa constància abans de morir. Sense això, un
# backup que falla només es nota el dia que fa falta.
fallar() {
  echo "  FALLO: $1" >> "$LOG"
  echo "[$DATE] BACKUP FALLIT" >> "$LOG"
  echo "---" >> "$LOG"
  echo "BACKUP FALLIT: $1" >&2
  exit 1
}

echo "[$DATE] Inici backup..." >> "$LOG"

# 1. PostgreSQL dump
# Es bolca a .tmp i només es reanomena si passa la verificació. Amb `set -e`,
# una redirecció `>` que mor a mitges deixava un .pgdump truncat que el
# restore.sh llistava com a vàlid.
echo "  Fent pg_dump..." >> "$LOG"
docker exec nfc2-docker-postgres-1 pg_dump -U nfc_app -d cleaning_service \
  --format=custom --compress=6 > "$BACKUP_FILE.pgdump.tmp" 2>> "$LOG" \
  || fallar "pg_dump ha retornat error"

MIDA=$(stat -c%s "$BACKUP_FILE.pgdump.tmp" 2>/dev/null || echo 0)
[ "$MIDA" -gt "$MIDA_MINIMA_DUMP" ] \
  || fallar "el dump fa només $MIDA bytes"

# La prova de debò: si pg_restore no pot llegir l'índex, el fitxer no serveix
# per restaurar, per gran que sigui.
docker exec -i nfc2-docker-postgres-1 pg_restore --list \
  < "$BACKUP_FILE.pgdump.tmp" > /dev/null 2>> "$LOG" \
  || fallar "el dump no es pot llegir (pg_restore --list)"

mv "$BACKUP_FILE.pgdump.tmp" "$BACKUP_FILE.pgdump"
PG_SIZE=$(du -sh "$BACKUP_FILE.pgdump" 2>/dev/null | cut -f1)
echo "  pg_dump OK i verificat: $PG_SIZE" >> "$LOG"

# 2. Fitxers: només uploads. Els secrets van a part (pas 3).
# Els adjunts de la missatgeria queden FORA del tar diari: amb 30 còpies,
# cada foto i cada vídeo es guardarien 30 vegades, i el gzip no comprimeix
# res que ja estigui comprimit. Es copien a part un cop per setmana (2b).
echo "  Comprimint fitxers..." >> "$LOG"
tar czf "$BACKUP_FILE.files.tar.gz.tmp" \
  --exclude='uploads/messaging' \
  -C "$APP_DIR" \
  uploads/ 2>/dev/null \
  || fallar "no s'ha pogut comprimir uploads/"

tar tzf "$BACKUP_FILE.files.tar.gz.tmp" > /dev/null 2>> "$LOG" \
  || fallar "el tar d'uploads surt corrupte"

mv "$BACKUP_FILE.files.tar.gz.tmp" "$BACKUP_FILE.files.tar.gz"
FILES_SIZE=$(du -sh "$BACKUP_FILE.files.tar.gz" 2>/dev/null | cut -f1)
echo "  Fitxers OK i verificat: $FILES_SIZE" >> "$LOG"

# 3. Secrets, a part i xifrats.
# Fins ara el .env i les claus anaven en clar dins del tar d'uploads: qui
# aconseguís una còpia tenia DB_PASSWORD, les claus d'API i la VAPID privada,
# i aquestes còpies han de poder sortir de l'edifici.
echo "  Desant secrets..." >> "$LOG"
tar czf "$BACKUP_FILE.secrets.tar.gz" \
  -C "$APP_DIR" \
  instance/.secret_key \
  instance/.jwt_secret_key \
  instance/.vapid_private_key \
  instance/.vapid_public_key \
  .env 2>/dev/null \
  || fallar "no s'han pogut empaquetar els secrets"

if [ -s "$PASS_FILE" ]; then
  gpg --symmetric --cipher-algo AES256 --batch --yes \
      --passphrase-file "$PASS_FILE" \
      --output "$BACKUP_FILE.secrets.tar.gz.gpg" \
      "$BACKUP_FILE.secrets.tar.gz" 2>> "$LOG" \
    || fallar "no s'han pogut xifrar els secrets"
  rm -f "$BACKUP_FILE.secrets.tar.gz"
  chmod 600 "$BACKUP_FILE.secrets.tar.gz.gpg"
  echo "  Secrets OK (xifrats)" >> "$LOG"
else
  # Sense contrasenya no es pot xifrar, però tampoc es pot deixar de copiar:
  # sense aquests fitxers una restauració no aixeca l'aplicació. Es deixen amb
  # permisos tancats i es crida l'atenció cada dia fins que algú ho arregli.
  chmod 600 "$BACKUP_FILE.secrets.tar.gz"
  echo "  AVIS: secrets SENSE XIFRAR. Crea $PASS_FILE (chmod 600) amb una" >> "$LOG"
  echo "        contrasenya llarga i guarda-la també fora del NAS." >> "$LOG"
  echo "AVIS: els secrets del backup no estan xifrats. Crea $PASS_FILE" >&2
fi

# Empremta de cada fitxer, per poder comprovar després que el que hi ha al disc
# extern o al núvol és exactament el que va sortir d'aquí.
(cd "$BACKUP_DIR" && sha256sum "$(basename "$BACKUP_FILE")".* \
  > "$(basename "$BACKUP_FILE")".sha256 2>/dev/null) || true

# 2b. Adjunts de la missatgeria: espill setmanal, sense comprimir.
# Un adjunt esborrat desapareix de l'espill, però el registre a la base de
# dades sí que es conserva 30 dies. Per a un xat és un compromís raonable,
# i és el que evita multiplicar per 30 uns quants GB.
if [ "$(date +%u)" = "7" ] && [ -d "$APP_DIR/uploads/messaging" ]; then
  echo "  Sincronitzant adjunts de missatgeria..." >> "$LOG"
  mkdir -p "$BACKUP_DIR/messaging_mirror"
  rsync -a --delete "$APP_DIR/uploads/messaging/" "$BACKUP_DIR/messaging_mirror/" >> "$LOG" 2>&1
  echo "  Adjunts OK: $(du -sh "$BACKUP_DIR/messaging_mirror" 2>/dev/null | cut -f1)" >> "$LOG"
fi

# 3. Neteja: eliminar backups > 30 dies
DELETED=$(find "$BACKUP_DIR" -name "backup_*" -mtime +30 -delete -print | wc -l)
echo "  Neteja: $DELETED fitxers antics eliminats" >> "$LOG"

# 4. Resum
TOTAL_BACKUPS=$(ls "$BACKUP_DIR"/backup_*.pgdump 2>/dev/null | wc -l)
TOTAL_SIZE=$(du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1)
echo "  Backup completat: $BACKUP_FILE (DB: $PG_SIZE, Files: $FILES_SIZE)" >> "$LOG"
echo "  Total backups: $TOTAL_BACKUPS, Espai total: $TOTAL_SIZE" >> "$LOG"
echo "---" >> "$LOG"

echo "Backup completat i verificat: $BACKUP_FILE"
