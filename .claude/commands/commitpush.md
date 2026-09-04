---
description: Hace commit de todos los cambios del proyecto y push a origin
allowed-tools: Bash(git status:*), Bash(git diff:*), Bash(git log:*), Bash(git add:*), Bash(git commit:*), Bash(git push:*), Bash(git branch:*), Bash(git rev-parse:*)
---

Haz commit y push de los cambios del proyecto.

## Contexto actual

- Rama: !`git branch --show-current`
- Estado: !`git status --short`
- Cambios ya en el índice: !`git diff --cached --stat`
- Cambios sin indexar: !`git diff --stat`
- Últimos commits (para imitar el estilo): !`git log -5 --format='%s'`

## Qué hacer

1. **Revisa el diff completo** (`git diff` y `git diff --cached`) para entender qué
   ha cambiado de verdad. Si no hay ningún cambio, dilo y termina.

2. **Comprueba antes de commitear:**
   - Que no se cuela ningún secreto: `.env`, `instance/.secret_key`,
     `instance/.jwt_secret_key`, `instance/.vapid_*`, `*.db`, claves de API.
   - Que no entran ficheros basura: `__pycache__/`, `.pytest_cache/`, `venv*/`,
     `*.apk`, `Thumbs.db`, ficheros temporales.
   - Si el cambio añade funcionalidad visible, que `app/templates/admin_help.html`
     esté actualizado (documentación + entrada en el changelog). Si falta, **avisa
     al usuario antes de commitear** en vez de commitear a medias.

3. **Prepara los ficheros:** `git add` de los ficheros de código relevantes. No uses
   `git add -A` a ciegas si hay ficheros dudosos en el estado; en ese caso añádelos
   uno a uno y comenta cuáles has dejado fuera.

4. **Escribe el mensaje** siguiendo el estilo del repositorio:
   - Asunto en **inglés**, imperativo, ≤ 72 caracteres
     (`Add ...`, `Fix ...`, `Refactor ...`).
   - Línea en blanco y un cuerpo de 1–3 líneas explicando el *por qué*, con líneas
     de ~72 caracteres.
   - Termina con:
     ```
     Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
     ```
   - Usa un heredoc para pasar el mensaje multilínea.

5. **Commit y push:**
   ```
   git commit -m "$(cat <<'EOF'
   ...mensaje...
   EOF
   )"
   git push
   ```
   Si la rama no tiene upstream, usa
   `git push -u origin $(git branch --show-current)`.

6. **Informa del resultado:** hash corto del commit, asunto, rama y confirmación de
   que el push ha ido bien. Si el push falla (rechazo por non-fast-forward, sin
   credenciales, sin red), explica el error y qué hacer — **no** hagas
   `push --force` ni reescribas historial.

7. **Recuerda el deploy al NAS.** Despues de informar del push, anade siempre un
   bloque final recordando al usuario como desplegar, aunque no lo pida:
   - El comando SSH de conexion al NAS.
   - El comando de deploy en **una sola linea** (descarga del zip + `docker build`
     + `docker-compose up -d`).
   - Si el commit incluye migraciones nuevas, el `flask db stamp <head>` con el head
     real (`flask db heads` en local); si no, decir explicitamente que no hace falta.
   - Si el commit toca columnas de tablas existentes, el `ALTER TABLE` manual.

   Los valores reales (host, puerto SSH, usuario, contenedores, directorio de
   deploy) **no se escriben en este fichero**: el repositorio es publico. Estan en
   la memoria local de Claude (`reference_nas_docker`) y en
   `.claude/rules/06-deploy-nas.md` como marcadores.

## Límites

- No toques `main` con force-push, rebase ni reset destructivo.
- No modifiques código para "arreglar" nada durante este comando: solo commit y push
  de lo que ya hay.
- Este comando **no despliega**. El deploy al NAS es un proceso aparte descrito en
  `.claude/rules/06-deploy-nas.md`.
