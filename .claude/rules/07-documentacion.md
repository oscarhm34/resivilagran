# Documentación y registro de cambios

## Regla principal

**Toda funcionalidad nueva o cambio visible para el usuario debe actualizar
`app/templates/admin_help.html` antes de commitear.** Dos cosas, no una:

1. **Documentación de uso** — añadir o ampliar la sección correspondiente
   (secciones 1–18) explicando cómo se usa la función.
2. **Entrada en el registro de cambios** — sección 19 (`id="changelog"`).

Los administradores de la residencia consultan esa página como manual único; si no se
actualiza, la función existe pero nadie sabe usarla.

## Formato del changelog

Agrupado por mes (`<h6>Agosto 2026</h6>`), entradas más recientes arriba, dentro de
una `<table class="table table-sm">`:

```html
<tr><td>21/08</td><td><strong>Titulo corto:</strong> descripcion de que hace la funcion y donde se encuentra en la interfaz.</td></tr>
```

- Fecha `DD/MM`.
- Título en `<strong>` seguido de dos puntos, luego la descripción.
- **En castellano** y en lenguaje de usuario, no técnico: describir qué ve y qué hace
  el administrador, no cómo está implementado.
- El texto del changelog existente se escribe **sin tildes** — mantener esa
  coherencia dentro de la tabla.
- Si el mes en curso no tiene aún su `<h6>`, crearlo encima de la tabla del mes
  anterior.

## Índice de secciones

El índice del principio de `admin_help.html` (`<li><a href="#...">`) debe reflejar
cualquier sección nueva. Si se añade una sección, renumerar el índice y el
`card-header` correspondiente.

## Otra documentación

- `docs/` — guías largas en Markdown (ej. `guia_cuadrantes_turnos.md`). Usar para
  procesos complejos que no caben en la ayuda en pantalla, y enlazarlos desde ella.
- `.env.example` — cada variable de entorno nueva se documenta ahí, con un valor de
  ejemplo, nunca el real.
- `.claude/rules/` — estas reglas. Actualizarlas cuando cambie una convención del
  proyecto, no cuando cambie una funcionalidad.
