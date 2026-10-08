#!/bin/bash
# =============================================================================
# Backup script per La Vila Gran - NFC App
# Executa diàriament via Synology Task Scheduler a les 03:00
#
# Què fa:
#   1. pg_dump de PostgreSQL i EL VERIFICA abans de donar-lo per bo
#   2. Comprimeix uploads (sense secrets)
#   3. Desa els secrets a part i xifrats
#   4. Puja una copia xifrada a C2 Object Storage (immutable, 30 dies)
#   5. Elimina backups > 30 dies

# Per recuperar des de la nuvol quan el NAS no hi sigui, veure la guia
# `guia-copia-nube.md` de la carpeta de seguretat: son dues ordres d'rclone i
# un openssl, i es poden executar des de qualsevol ordinador.
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
# Còpia fora de l'edifici, a C2 Object Storage. Si el fitxer de configuració no
# hi és, l'script fa la còpia local igual i salta aquest pas.
RCLONE_CONF="${RCLONE_CONF:-/volume1/docker/.rclone.conf}"
C2_DESTI="${C2_DESTI:-c2:lavilagran-copias/nas-residencia}"
# Quants dies es conserven a la núvol. Ha de ser MÉS que el bloqueig del
# depòsit (30 dies), o l'esborrat xocarà contra l'Object Lock cada nit.
C2_DIES=45
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/backup_$DATE"
LOG="$BACKUP_DIR/backup.log"

mkdir -p "$BACKUP_DIR"
# Aquestes còpies porten tot l'historial clínic i totes les fotos dels
# residents. Només el propietari.
chmod 700 "$BACKUP_DIR" 2>/dev/null || true

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
#
# Només s'empaqueta el que existeix de debò: SECRET_KEY i JWT_SECRET_KEY poden
# viure al .env en comptes de a instance/, i llavors aquells dos fitxers no
# existeixen enlloc. La versió anterior els demanava sempre, el tar sortia amb
# error cada nit i ningú se n'assabentava perquè anava tot a /dev/null.
echo "  Desant secrets..." >> "$LOG"
SECRETS=""
for f in instance/.secret_key instance/.jwt_secret_key \
         instance/.vapid_private_key instance/.vapid_public_key .env; do
  # Amb `if` i no amb `&&`: si l'últim de la llista no existís, l'`&&` deixaria
  # el bucle amb codi d'error i `set -e` mataria l'script sense dir per què.
  if [ -e "$APP_DIR/$f" ]; then
    SECRETS="$SECRETS $f"
  else
    echo "  (no hi ha $f, se salta)" >> "$LOG"
  fi
done
[ -n "$SECRETS" ] || fallar "no s'ha trobat cap fitxer de secrets"

# Sense cometes a propòsit: $SECRETS és una llista d'arguments, no un sol nom.
# shellcheck disable=SC2086
tar czf "$BACKUP_FILE.secrets.tar.gz" -C "$APP_DIR" $SECRETS \
  || fallar "no s'han pogut empaquetar els secrets"
echo "  Secrets inclosos:$SECRETS" >> "$LOG"

# Es xifra amb openssl i no amb gpg: el gpg del NAS vol crear-se un directori de
# treball al home de l'usuari, i aquest usuari no en té («can't create directory
# /var/services/homes/...»). openssl no necessita ni clauer ni pinentry.
# -pbkdf2 amb moltes iteracions perquè la clau no surti d'un hash pelat.
if [ -s "$PASS_FILE" ]; then
  if openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -salt \
       -pass "file:$PASS_FILE" \
       -in "$BACKUP_FILE.secrets.tar.gz" \
       -out "$BACKUP_FILE.secrets.tar.gz.enc" 2>> "$LOG"; then
    rm -f "$BACKUP_FILE.secrets.tar.gz"
    chmod 600 "$BACKUP_FILE.secrets.tar.gz.enc"
    echo "  Secrets OK (xifrats)" >> "$LOG"
  else
    # Si el xifrat falla, el tar en clar no es pot quedar al disc: és just el
    # fitxer que no volem que hi hagi.
    rm -f "$BACKUP_FILE.secrets.tar.gz" "$BACKUP_FILE.secrets.tar.gz.enc"
    fallar "no s'han pogut xifrar els secrets"
  fi
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

# El dump i les fotos també són dades de salut: no poden quedar llegibles per
# qualsevol que entri al NAS, com passava fins ara (rwxrwxrwx).
chmod 600 "$BACKUP_FILE".* 2>/dev/null || true

# 4. Fora de l'edifici: C2 Object Storage, amb Object Lock de 30 dies.
#
# Es puja amb rclone i no amb Hyper Backup per dos motius. El primer, pràctic:
# en aquest NAS Hyper Backup no connecta amb C2 (falla després de llistar el
# depòsit, i té dues tasques velles en error). El segon, de fons: Hyper Backup
# reescriu el seu conjunt de còpies, i això xoca amb un depòsit bloquejat, que
# per definició no deixa reescriure res. Aquests fitxers, en canvi, porten la
# data al nom i no es toquen mai més: són exactament el que un magatzem
# immutable espera.
#
# Tot el que surt de l'edifici va xifrat. El dump i les fotos són dades de
# salut i C2 és un tercer; que només hi arribi un bloc il·legible no és un
# extra, és la condició per poder-ho enviar.
if [ -s "$RCLONE_CONF" ] && [ -s "$PASS_FILE" ]; then
  echo "  Pujant a la nuvol..." >> "$LOG"
  PUJADA="$BACKUP_DIR/.pujada"
  rm -rf "$PUJADA"
  mkdir -p "$PUJADA"

  for f in "$BACKUP_FILE.pgdump" "$BACKUP_FILE.files.tar.gz"; do
    [ -f "$f" ] || continue
    openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -salt \
      -pass "file:$PASS_FILE" -in "$f" -out "$PUJADA/$(basename "$f").enc" \
      || { rm -rf "$PUJADA"; fallar "no s'ha pogut xifrar $(basename "$f")"; }
  done
  # Els secrets ja estan xifrats, i el sha256 no és cap secret.
  cp "$BACKUP_FILE.secrets.tar.gz.enc" "$PUJADA/" 2>/dev/null || true
  cp "$BACKUP_FILE.sha256" "$PUJADA/" 2>/dev/null || true

  # --s3-no-check-bucket: la clau d'accés només té permisos sobre el depòsit,
  # no per comprovar-ne l'existència. Sense això rclone falla abans de pujar.
  if docker run --rm \
       -v "$RCLONE_CONF":/config/rclone/rclone.conf:ro \
       -v "$PUJADA":/data:ro \
       rclone/rclone copy /data "$C2_DESTI/" \
       --s3-no-check-bucket --transfers 2 >> "$LOG" 2>&1; then
    echo "  Nuvol OK: $(ls "$PUJADA" | wc -l) fitxers" >> "$LOG"
  else
    rm -rf "$PUJADA"
    fallar "no s'ha pogut pujar a la nuvol"
  fi
  rm -rf "$PUJADA"

  # Neteja del que ja ha passat el bloqueig. Si encara està bloquejat, C2 ho
  # rebutja i no passa res: per això el `|| true`, que un esborrat fallit no
  # pot carregar-se un backup que ja és bo.
  docker run --rm \
    -v "$RCLONE_CONF":/config/rclone/rclone.conf:ro \
    rclone/rclone delete "$C2_DESTI/" --min-age "${C2_DIES}d" \
    --s3-no-check-bucket >> "$LOG" 2>&1 || true
else
  echo "  (sense còpia a la nuvol: falta $RCLONE_CONF o $PASS_FILE)" >> "$LOG"
fi

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
