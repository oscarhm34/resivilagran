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
  nuevo se consulta antes. **Meta (WhatsApp)** es un destino previsto solo para los
  horarios del personal: recibe el nombre de la trabajadora, su número y un enlace.
  Nunca el horario dentro del mensaje, y nunca nada de residentes.
- Las fotos y selfies van a `uploads/` (fuera de `static/`) y se sirven mediante
  rutas controladas, nunca por ruta estática directa.

## Modelo de autenticación dual

| Superficie | Mecanismo | Sesión |
|---|---|---|
| Panel admin | Flask-Login + `@admin_required` | Cookie de sesión |
| PWA / app trabajadoras | Flask-JWT-Extended, token en `localStorage` | Bearer en header |
| Enlace personal (horario por WhatsApp) | Token firmado con `itsdangerous` | Ninguna |

`Cleaner` es el único modelo de usuario: `is_admin` distingue el rol administrativo y
`role` (`limpieza`, `atenciones`, `mixto`, `gestion`) el funcional. `active=False`
debe excluir al usuario de listados y de poder operar.

`_verify_worker_id()` es obligatorio siempre que un endpoint JWT reciba un
`worker_id` en el cuerpo o la query: sin él, cualquier trabajadora autenticada podría
operar sobre registros de otra.

### La tercera forma: el enlace firmado

`/horario/<token>` y `/horario/<token>/confirmar` (en `blueprints/shifts.py`) son las
**únicas rutas públicas** del proyecto. Las abre una trabajadora desde un enlace que le
llega por WhatsApp, en un móvil sin sesión y sin la aplicación instalada, así que no hay
cookie ni Bearer que comprobar: lo que autoriza es la firma.

Un enlace firmado vale como autorización solo si cumple **las cuatro condiciones**:

1. Token de `itsdangerous.URLSafeTimedSerializer` con `SECRET_KEY` y un `salt` propio
   (`_FIRMA_HORARIO`). Cambiar un carácter invalida la firma.
2. **Caduca** (`max_age`; hoy 90 días). Un enlace eterno es una credencial eterna.
3. Lleva dentro **a quién pertenece y a qué se refiere**, y la ruta no acepta ningún
   identificador más por la URL ni por el cuerpo: así no sirve para ver lo de otra.
4. **Ni un dato de residentes.** Es la línea que no se cruza al abrir algo a internet.
   Solo lo que ya sabe quien recibe el enlace: su nombre y sus propias horas.

Dos cosas que hay que tener presentes y que **no** son propias de esta función:

- Si se pierde `instance/`, `SECRET_KEY` se regenera y **todos los enlaces mandados
  dejan de valer**. Hay que volver a enviarlos. Mismo aviso que para las claves VAPID.
- Sin `ProxyFix` todas las visitas llegan con la IP del proxy, así que un límite de
  Flask-Limiter en estas rutas sería global de hecho. Contra el token no importa —son
  cien caracteres firmados—, pero conviene saberlo antes de confiar en un límite por IP.

Añadir una ruta pública nueva **no** es rutina: se acuerda antes, como cualquier destino
externo de datos.

## CSRF

Los blueprints están **exentos de CSRF** (la lista de `csrf.exempt(bp)` al final de
`app/__init__.py`, hoy 16) porque la app se pensó para la red local y las rutas API
usan Bearer JWT. La protección real de los formularios admin viene del token que
`base.html` inyecta por JS, y `dual_auth` (`app/utils.py`) exige además la cabecera
`X-CSRFToken` en las escrituras autenticadas por cookie.

**La condición que justificaba esto ya no se cumple:** la aplicación está expuesta en
internet por el proxy del NAS y hay una ruta pública (`/horario/<token>`). La decisión
está pendiente de revisión, y lo que se contempla no es quitar la exención blueprint a
blueprint —casi todos mezclan rutas de panel y de PWA, así que romperían la PWA— sino
un guardián global que deje pasar los `GET` y todo lo que traiga `Authorization: Bearer`
y exija el token al resto. Antes hace falta arreglar `login.html`, que tiene un
formulario sin token y no extiende `base.html`.

Si se añade un blueprint nuevo, hay que añadirlo a esa lista o sus formularios fallarán.

## Secretos

- `SECRET_KEY`, `JWT_SECRET_KEY` y las claves VAPID se leen de entorno y, si faltan,
  se generan y persisten en `instance/` (`.secret_key`, `.jwt_secret_key`,
  `.vapid_*`). Esos ficheros **no** se commitean y **sí** entran en el backup.
- `ANTHROPIC_API_KEY`, `WHATSAPP_TOKEN` y `DB_PASSWORD` solo por `.env`. `.env` nunca al repositorio;
  `.env.example` documenta las variables sin valores reales.
- Nunca hardcodear credenciales en código, plantillas ni tests.

## Subida de ficheros

- Validar extensión con `_allowed_file(filename, ALLOWED_DOC_EXTENSIONS |
  ALLOWED_IMAGE_EXTENSIONS)`.
- Las imágenes en base64 se reprocesan con Pillow (`_save_base64_photo` en
  `blueprints/nfc.py`): convierte a RGB, reduce a 800px y reescribe como JPEG. Ese
  reprocesado es la defensa contra ficheros maliciosos disfrazados de imagen —
  mantenerlo.
- `MAX_CONTENT_LENGTH` = **24 MB** (`app/config.py`), no 16: el tope de un vídeo
  de mensajería son 15 MB y el multipart añade lo suyo. El tope real por tipo de
  fichero se comprueba en `blueprints/messaging.py`. Nombres de fichero generados
  por el servidor (`{cleaner_id}_{timestamp}.jpg`), nunca el que envía el cliente.

## Registro de autenticación

Toda entrada, salida e intento fallido se registra con `log_auth()` de `app/utils.py`,
en `AuditLog` con `table_name='auth'`. A diferencia de `log_audit()`, **hace su propio
commit**: un evento de autenticación no pertenece a ninguna transacción de negocio y no
puede perderse en el rollback de otra cosa.

- **Nunca** se registra la contraseña probada, ni su longitud, ni una pista de ella.
- Al usuario se le dice siempre lo mismo (para no revelar qué nombres existen); el
  motivo real (`usuario inexistente`, `contraseña incorrecta`, `cuenta desactivada`,
  `sin permisos`) va solo a la auditoría.
- `log_audit()` resuelve el autor en cascada (`current_dual_user` → `current_user` →
  JWT) y **espeja la entrada al registro del servidor** antes de escribirla, con las
  claves de `details` pero nunca sus valores: pueden llevar nombres de residentes.

Cualquier endpoint de autenticación nuevo debe registrar sus eventos.

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
