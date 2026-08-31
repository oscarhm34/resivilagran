# Deploy en el NAS Synology

Producción: NAS Synology con Docker Compose (Flask + PostgreSQL 15), detrás del
proxy inverso del propio NAS.

> Los valores reales de host, puerto SSH, usuario, IPs y nombres de contenedor
> **no se versionan** (el repositorio es público). Están en la memoria local de
> Claude y en las notas del administrador. En este documento aparecen como
> `<NAS_HOST>`, `<SSH_PORT>`, `<SSH_USER>`, `<CONTENEDOR>`, `<DEPLOY_DIR>`.

## Comandos SSH: siempre en UNA sola línea

El usuario copia y pega los comandos en la terminal SSH del NAS. Los saltos de línea
se pierden y provocan `SyntaxError` / `IndentationError`.

- Encadenar con `;` o `&&`, nunca en varias líneas.
- Python inline: `python -c "stmt1; stmt2; stmt3"`, bucles como list comprehensions.

## Proceso de deploy

El NAS **no tiene git**. Se descarga el zip de GitHub:

1. `git push` desde local.
2. `ssh -p <SSH_PORT> <SSH_USER>@<NAS_HOST>`
3. Descargar, descomprimir y reconstruir (una línea):

```
cd <DEPLOY_DIR> && curl -L https://github.com/oscarhm34/resivilagran/archive/refs/heads/main.zip -o main.zip && 7z x -y main.zip && cp -rf resivilagran-main/app resivilagran-main/run.py resivilagran-main/requirements.txt resivilagran-main/Dockerfile resivilagran-main/.dockerignore resivilagran-main/docker-compose.yml resivilagran-main/migrations . && rm -rf resivilagran-main main.zip && sudo docker build --no-cache -t nfc2-docker-nfc <DEPLOY_DIR> && docker-compose up -d --force-recreate nfc
```

**Nunca `cp -rf resivilagran-main/* .`** — sobrescribe `uploads/` e `instance/` y
borra fotos, la BD y los secretos. Copiar solo los ficheros de código listados.

**El build necesita `sudo`** (comprobado el 31/08/2026). El directorio de deploy
contiene `pgdata/`, el volumen de PostgreSQL, que pertenece al usuario del contenedor
(uid 999, permisos 700). El cliente Docker recorre el contexto y muere con
`error checking context: can't stat '<DEPLOY_DIR>/pgdata'`.
**Ponerlo en `.dockerignore` NO lo evita:** esta versión del cliente valida el contexto
antes de aplicar las exclusiones. Aun así `.dockerignore` sigue haciendo falta, para que
`COPY . .` no meta `pgdata/`, `uploads/` e `instance/` dentro de la imagen.

Pasar la **ruta de contexto completa**, no `.`: el punto final se pierde con facilidad al
copiar y pegar, y `docker build` falla con `requires exactly 1 argument`.

Arreglo de fondo pendiente: sacar `pgdata` del directorio de build (moverlo fuera o
pasarlo a volumen con nombre en `docker-compose.yml`) y así quitar el `sudo`. Toca la
base de datos: hacerlo con backup del día verificado, nunca en mitad de un deploy.

`docker-compose build` falla en este NAS (`mkdir /var/services/homes: file exists`);
usar `docker build` directamente.

## Migraciones en producción: `stamp`, nunca `upgrade`

`run.py` ejecuta `db.create_all()` al arrancar, así que tras el rebuild las tablas ya
existen y `flask db upgrade` **siempre** falla con `table already exists`.

```
docker exec <CONTENEDOR> flask db stamp <última_revision_id>
```

Para **columnas nuevas en tablas existentes** (que `create_all` no añade), ALTER
manual en una línea contra PostgreSQL:

```
docker exec <CONTENEDOR_POSTGRES> psql -U nfc_app -d cleaning_service -c "ALTER TABLE <tabla> ADD COLUMN <columna> <tipo>"
```

## Rollback

Redesplegar el zip de un commit anterior (mismo comando, sustituyendo
`refs/heads/main.zip` por `<COMMIT_HASH>.zip`), o `git revert` + deploy normal.

## Diagnóstico

```
docker logs <CONTENEDOR> --tail 100
docker exec -it <CONTENEDOR> bash
```

## Backups

`backup.sh` corre cada día a las 03:00 desde el Task Scheduler de Synology
(pg_dump + `uploads/` + secretos), con retención de 30 días en el directorio
`backups/` del deploy. Restauración interactiva con `./restore.sh <nombre_backup>`.

**Antes de cualquier operación destructiva en producción** (migración de datos,
borrado masivo, cambio de esquema), verificar que existe backup del día.

## Credenciales

La contraseña de PostgreSQL se pasa por `DB_PASSWORD` en el `.env` del NAS. El valor
por defecto que aparece en `docker-compose.yml` y `.env.example` **no debe usarse en
producción**: está publicado en el repositorio.
