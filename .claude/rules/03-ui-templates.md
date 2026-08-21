# UI, plantillas y diseño

## Idioma: castellano, siempre

**Todo** el texto visible —labels, botones, placeholders, toasts, mensajes de error,
títulos, changelog de la ayuda— va en **castellano**. Nunca en catalán ni en inglés.
Los comentarios de código y los mensajes de commit pueden ir en inglés; lo que ve el
usuario final, no.

## Dos interfaces separadas

### 1. Panel admin — `base.html` + ~50 plantillas

- Bootstrap **5.3.3** + Bootstrap Icons 1.11.3 (CDN).
- Tipografía: **Inter** (cuerpo, 15px) y **Outfit** (headings), vía Google Fonts.
- Toda plantilla nueva del admin **extiende `base.html`** y define
  `{% block title %}` y el bloque de contenido.

### 2. PWA de trabajadoras — `worker.html`

SPA de un solo fichero (~4.200 líneas) que **no** extiende `base.html`. Usa Web NFC,
JWT en `localStorage` y su propio set de variables CSS (`--blue`, `--green`, `--bg`,
`--surface`, `--text`, `--border`), ancho máximo 480px.

Las usuarias son limpiadoras con poca experiencia tecnológica, en móvil Android:
**targets táctiles grandes, texto legible, pocos pasos, tolerancia a errores.**
No usar `confirm()`/`alert()` nativos: rompen la experiencia de PWA.

## Tokens de diseño — usarlos, no inventar colores

Definidos en `:root` de `base.html`. Nunca hardcodear un hex en una plantilla nueva:

```
--color-primary #0069d9   --color-primary-dk #004fa3   --color-primary-lt #e8f1fb
--color-success #1a7f37   --color-warning #bf8700       --color-danger #cf222e
--color-bg #f4f6f9        --color-surface #ffffff       --color-border #d0d7de
--color-text #1f2328      --color-text-secondary #656d76
--radius-sm/md/lg 4/8/12px   --shadow-sm  --shadow-md   --navbar-height 62px
```

## Modo oscuro — obligatorio

El tema se aplica con `data-theme="dark"` en `<html>` (persistido en `localStorage`,
script inline en el `<head>` para evitar flash). Cada componente nuevo que use color
propio necesita su override en el bloque `[data-theme="dark"]` de `base.html`.
Comprobar siempre el resultado en claro **y** en oscuro antes de dar algo por hecho.

## CSS

Estilos compartidos → bloque `<style>` de `base.html`. Estilos de un solo uso →
`{% block styles %}` de la plantilla. Evitar `<style>` locales que redefinan tokens:
ya hay deuda de ese tipo (`login.html` duplica los tokens, `index.html` define
`.stat-card`/`.home-card` en local). No ampliarla.

## Accesibilidad

- Modales con `aria-labelledby` apuntando al título.
- Botones con solo icono → `aria-label` o `title`.
- Los estados no se comunican solo por color: `.status-badge` lleva punto indicador
  y texto.

## CSRF en formularios admin

`base.html` expone `<meta name="csrf-token">` y lo auto-inyecta en las peticiones JS.
En formularios `<form method="POST">` del admin, incluir el token si el blueprint no
está exento. Ver [04-seguridad.md](04-seguridad.md).

## Datos en plantillas

Jinja2 autoescapa por defecto: **no usar `|safe`** sobre nada que provenga de la BD o
del usuario. Los cálculos y agregaciones se hacen en Python, no con lógica compleja
dentro de la plantilla.
