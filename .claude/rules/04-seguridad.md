# Seguridad y datos sensibles

## Contexto: datos de salud

La aplicación gestiona **datos de salud de residentes de una residencia de tercera
edad** (medicación, valoraciones Barthel/Norton/Pfeiffer, heridas, constantes
vitales, caídas, incidencias, fotos). Son datos de categoría especial bajo el RGPD.

Consecuencias prácticas:

- Ningún endpoint que devuelva datos de residentes sin decorador de autorización.
- No volcar datos de residentes en logs (`app.logger`), ni en mensajes de error, ni
  en respuestas de depuración.
- No enviar datos de residentes a servicios externos salvo los ya previstos
  (Anthropic vía `chatbot.py`, Resiplus vía `resiplus_client.py`). Cualquier destino
  nuevo se consulta antes.
- Las fotos y selfies van a `uploads/` (fuera de `static/`) y se sirven mediante
  rutas controladas, nunca por ruta estática directa.

## Modelo de autenticación dual

| Superficie | Mecanismo | Sesión |
|---|---|---|
| Panel admin | Flask-Login + `@admin_required` | Cookie de sesión |
| PWA / app trabajadoras | Flask-JWT-Extended, token en `localStorage` | Bearer en header |

`Cleaner` es el único modelo de usuario: `is_admin` distingue el rol administrativo y
`role` (`limpieza`, `atenciones`, `mixto`, `gestion`) el funcional. `active=False`
debe excluir al usuario de listados y de poder operar.

`_verify_worker_id()` es obligatorio siempre que un endpoint JWT reciba un
`worker_id` en el cuerpo o la query: sin él, cualquier trabajadora autenticada podría
operar sobre registros de otra.

## CSRF

Los 14 blueprints están **exentos de CSRF** (`csrf.exempt(bp)` en `app/__init__.py`)
porque la app es de red local y las rutas API usan Bearer JWT. La protección real de
los formularios admin viene del token que `base.html` inyecta por JS.

Esto es una decisión deliberada y documentada. Si se añade un blueprint nuevo, hay
que añadirlo a esa lista o sus formularios fallarán. Si en algún momento la app se
expone fuera de la red local, esta decisión debe revisarse.

## Secretos

- `SECRET_KEY`, `JWT_SECRET_KEY` y las claves VAPID se leen de entorno y, si faltan,
  se generan y persisten en `instance/` (`.secret_key`, `.jwt_secret_key`,
  `.vapid_*`). Esos ficheros **no** se commitean y **sí** entran en el backup.
- `ANTHROPIC_API_KEY` y `DB_PASSWORD` solo por `.env`. `.env` nunca al repositorio;
  `.env.example` documenta las variables sin valores reales.
- Nunca hardcodear credenciales en código, plantillas ni tests.

## Subida de ficheros

- Validar extensión con `_allowed_file(filename, ALLOWED_DOC_EXTENSIONS |
  ALLOWED_IMAGE_EXTENSIONS)`.
- Las imágenes en base64 se reprocesan con Pillow (`_save_base64_photo` en
  `blueprints/nfc.py`): convierte a RGB, reduce a 800px y reescribe como JPEG. Ese
  reprocesado es la defensa contra ficheros maliciosos disfrazados de imagen —
  mantenerlo.
- `MAX_CONTENT_LENGTH` = 16 MB. Nombres de fichero generados por el servidor
  (`{cleaner_id}_{timestamp}.jpg`), nunca el nombre que envía el cliente.

## Rate limiting

Flask-Limiter con `default_limits=[]` (sin límite global). Se aplica explícitamente
donde importa: login admin y login worker (`10/minute`), chat (`10/minute`),
endpoints de escritura costosos (`5/minute`). Todo endpoint de autenticación nuevo
debe llevar límite.

**Almacenamiento en memoria** (`storage_uri="memory://"`): los contadores se pierden
al reiniciar el contenedor y no se comparten entre workers.

## Redirecciones

El parámetro `next` del login se valida antes de redirigir (debe empezar por `/`, sin
`//` ni `:`). Aplicar la misma validación en cualquier redirección basada en
parámetros de la petición.

Para devolver al usuario a donde estaba, **`volver_atras(destino_por_defecto)`** de
`app/utils.py`, nunca `redirect(request.referrer or ...)` directo: el `Referer` lo
pone quien enlaza a la página, así que sin validarlo un formulario del panel salta a
otra web. Ojo con `//otra-web`, que es una URL absoluta con el esquema heredado y se
cuela si solo se comprueba que empiece por `/`.
