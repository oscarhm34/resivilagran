"""Traduccion del contenido que escribe coordinacion.

La webapp ya se ve en arabe, frances e ingles, pero lo que se traduce ahi son
los textos de la propia aplicacion. Lo que escribe coordinacion —las
instrucciones de un tipo de atencion, los items del checklist, la informacion
relevante de un residente— seguia saliendo en castellano, y es justamente el
texto que una trabajadora necesita entender para hacer bien su trabajo.

Se traduce con IA y se puede corregir a mano, igual que ya hace la formacion. La
IA no es de fiar para un texto de seguridad: por eso cada traduccion se puede
editar, y las que nadie ha repasado salen marcadas.

Si despues alguien cambia el original en castellano, la traduccion se marca como
desfasada y **deja de servirse**: se vuelve a ensenar el castellano hasta que se
rehaga. Una instruccion que ya no dice lo que dice la original es peor que no
tener traduccion.
"""
from __future__ import annotations

import json

from flask import Blueprint, request, jsonify, render_template

from .. import app, db, limiter
from ..models import (CareType, ChecklistItem, Resident, ContentTranslation)
from ..utils import (admin_required, _safe_commit, log_audit, APP_LANGUAGES,
                     CAMPOS_TRADUCIBLES, _huella)

bp = Blueprint('translations', __name__)


# ── Que hay traducible y como se llama ───────────────────────────────────────

# El modelo de cada tipo y como se titula cada campo en el panel. La lista de
# campos manda desde `CAMPOS_TRADUCIBLES` (app/utils.py): aqui solo se les pone
# nombre y se dice donde estan.
ENTIDADES = {
    'care_type': {
        'modelo': CareType,
        'etiqueta': 'Tipos de atención',
        'titulo': lambda o: o.name,
        'campos': {'name': 'Nombre', 'instructions': 'Instrucciones'},
    },
    'checklist_item': {
        'modelo': ChecklistItem,
        'etiqueta': 'Checklist de limpieza',
        'titulo': lambda o: o.text,
        'campos': {'text': 'Texto'},
    },
    'resident': {
        'modelo': Resident,
        'etiqueta': 'Residentes',
        'titulo': lambda o: o.name,
        'campos': {'relevant_info': 'Información relevante',
                   'allergies': 'Alergias'},
    },
}


def _objetos(entity_type: str):
    """Las filas de ese tipo que tienen algo que traducir."""
    cfg = ENTIDADES[entity_type]
    consulta = cfg['modelo'].query
    if hasattr(cfg['modelo'], 'active'):
        consulta = consulta.filter_by(active=True)
    filas = consulta.all()
    campos = CAMPOS_TRADUCIBLES[entity_type]
    return [o for o in filas if any((getattr(o, c, '') or '').strip()
                                    for c in campos)]


def _estado(entity_type: str, objetos) -> dict:
    """Para cada (id, campo, idioma): la traduccion y si esta al dia.

    Se cargan todas de una vez: la pantalla lista decenas de filas por cuatro
    idiomas, y una consulta por celda seria un N+1 de manual.
    """
    ids = [o.id for o in objetos]
    if not ids:
        return {}
    filas = ContentTranslation.query.filter(
        ContentTranslation.entity_type == entity_type,
        ContentTranslation.entity_id.in_(ids),
    ).all()
    return {(f.entity_id, f.field, f.lang): f for f in filas}


# ── Pantalla ─────────────────────────────────────────────────────────────────

@bp.route('/traducciones')
@bp.route('/traducciones/<entity_type>')
@admin_required
def manage_translations(entity_type: str = 'care_type'):
    if entity_type not in ENTIDADES:
        entity_type = 'care_type'
    cfg = ENTIDADES[entity_type]
    objetos = _objetos(entity_type)
    trads = _estado(entity_type, objetos)

    # Se prepara aqui y no en la plantilla: la logica de "esta desfasada" no
    # cabe en un Jinja legible.
    filas = []
    for o in objetos:
        for campo in CAMPOS_TRADUCIBLES[entity_type]:
            original = (getattr(o, campo, '') or '').strip()
            if not original:
                continue
            huella = _huella(original)
            idiomas = {}
            for code in APP_LANGUAGES:
                if code == 'es':
                    continue
                t = trads.get((o.id, campo, code))
                idiomas[code] = {
                    'texto': t.text if t else '',
                    'desfasada': bool(t and t.source_hash != huella),
                    'revisada': bool(t and t.reviewed),
                }
            filas.append({
                'id': o.id, 'titulo': cfg['titulo'](o), 'campo': campo,
                'campo_nombre': cfg['campos'][campo], 'original': original,
                'idiomas': idiomas,
            })

    pendientes = sum(1 for f in filas for d in f['idiomas'].values()
                     if not d['texto'] or d['desfasada'])
    return render_template(
        'manage_translations.html',
        entity_type=entity_type, entidades=ENTIDADES, filas=filas,
        idiomas={c: d for c, d in APP_LANGUAGES.items() if c != 'es'},
        pendientes=pendientes)


# ── Traducir con IA ──────────────────────────────────────────────────────────

def _texto_original(entity_type: str, entity_id: int, field: str):
    """El castellano de ese campo, o None si no vale para traducir."""
    if entity_type not in ENTIDADES or field not in CAMPOS_TRADUCIBLES[entity_type]:
        return None
    obj = db.session.get(ENTIDADES[entity_type]['modelo'], entity_id)
    if not obj:
        return None
    return (getattr(obj, field, '') or '').strip() or None


@bp.route('/api/traducciones/generar', methods=['POST'])
@admin_required
# Cada pulsacion es una llamada a Anthropic que se paga. El limite no molesta a
# nadie traduciendo a mano y evita que un dedo pegado en el boton se cobre.
@limiter.limit("20/minute", methods=["POST"])
def generar_traduccion():
    """Traduce un campo a los idiomas pedidos con IA."""
    from .assessments import _call_claude

    data = request.json or {}
    entity_type = data.get('entity_type', '')
    field = data.get('field', '')
    try:
        entity_id = int(data.get('entity_id', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Identificador no válido'}), 400

    original = _texto_original(entity_type, entity_id, field)
    if not original:
        return jsonify({'error': 'No hay texto que traducir'}), 404

    langs = [l for l in (data.get('langs') or [])
             if l in APP_LANGUAGES and l != 'es']
    if not langs:
        return jsonify({'error': 'Selecciona al menos un idioma'}), 400

    nombres = ', '.join(f"{c} ({APP_LANGUAGES[c]['name']})" for c in langs)
    system = (
        "Eres traductor profesional para una residencia de personas mayores. "
        "Traduces textos de trabajo dirigidos a personal de limpieza y atencion "
        "que puede tener poca formacion. Usa lenguaje llano, frases cortas y "
        "trato de usted. No anadas explicaciones, advertencias ni cambies el "
        "significado: es texto que se sigue al pie de la letra mientras se "
        "atiende a una persona mayor. Manten los nombres propios sin traducir. "
        "Responde SOLO con un objeto JSON, sin texto alrededor, con la forma "
        '{"ar": "...", "fr": "..."}'
    )
    prompt = (f"Traduce este texto a los idiomas {nombres}.\n\n"
              f"Texto original en castellano:\n{original}")

    try:
        respuesta = _call_claude(system, prompt)
        import re
        match = re.search(r'\{.*\}', respuesta, re.DOTALL)
        if not match:
            return jsonify({'error': 'La IA no ha devuelto una traducción válida.'}), 502
        payload = json.loads(match.group())
    except (ValueError, json.JSONDecodeError) as e:
        app.logger.error('Error al traducir contenido: %s', e)
        return jsonify({'error': 'No se ha podido generar la traducción.'}), 502

    huella = _huella(original)
    hechas = {}
    for lang in langs:
        texto = (payload.get(lang) or '').strip()
        if not texto:
            continue
        _guardar(entity_type, entity_id, field, lang, texto, huella,
                 revisada=False)
        hechas[lang] = texto

    if not hechas:
        return jsonify({'error': 'La IA no ha devuelto ninguna traducción utilizable.'}), 502

    log_audit('translate', entity_type, entity_id,
              {'campo': field, 'idiomas': sorted(hechas)})
    ok, error = _safe_commit('Error al guardar las traducciones')
    if not ok:
        return jsonify({'error': error}), 500
    return jsonify({'ok': True, 'traducciones': hechas}), 200


def _guardar(entity_type, entity_id, field, lang, texto, huella, revisada):
    """Crea o actualiza una traduccion. No hace commit."""
    fila = ContentTranslation.query.filter_by(
        entity_type=entity_type, entity_id=entity_id,
        field=field, lang=lang).first()
    if fila is None:
        fila = ContentTranslation(entity_type=entity_type, entity_id=entity_id,
                                  field=field, lang=lang)
        db.session.add(fila)
    fila.text = texto
    fila.source_hash = huella
    fila.reviewed = revisada
    from datetime import datetime
    fila.generated_at = datetime.now()
    return fila


# ── Corregir a mano ──────────────────────────────────────────────────────────

@bp.route('/api/traducciones/guardar', methods=['POST'])
@admin_required
def guardar_traduccion():
    """Guarda una traduccion escrita o corregida por una persona.

    Queda marcada como revisada: es la diferencia entre un texto que ha mirado
    alguien y uno que solo ha pasado por la IA.
    """
    data = request.json or {}
    entity_type = data.get('entity_type', '')
    field = data.get('field', '')
    lang = data.get('lang', '')
    texto = (data.get('text') or '').strip()
    try:
        entity_id = int(data.get('entity_id', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Identificador no válido'}), 400

    if lang not in APP_LANGUAGES or lang == 'es':
        return jsonify({'error': 'Idioma no disponible'}), 400
    original = _texto_original(entity_type, entity_id, field)
    if not original:
        return jsonify({'error': 'No hay texto que traducir'}), 404
    if not texto:
        return jsonify({'error': 'El texto no puede estar vacío'}), 400

    _guardar(entity_type, entity_id, field, lang, texto, _huella(original),
             revisada=True)
    log_audit('update', 'content_translation', entity_id,
              {'tipo': entity_type, 'campo': field, 'idioma': lang})
    ok, error = _safe_commit('Error al guardar la traducción')
    if not ok:
        return jsonify({'error': error}), 500
    return jsonify({'ok': True}), 200


@bp.route('/api/traducciones/borrar', methods=['POST'])
@admin_required
def borrar_traduccion():
    data = request.json or {}
    try:
        entity_id = int(data.get('entity_id', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Identificador no válido'}), 400
    fila = ContentTranslation.query.filter_by(
        entity_type=data.get('entity_type', ''), entity_id=entity_id,
        field=data.get('field', ''), lang=data.get('lang', '')).first()
    if not fila:
        return jsonify({'error': 'No encontrada'}), 404
    db.session.delete(fila)
    log_audit('delete', 'content_translation', entity_id,
              {'tipo': data.get('entity_type'), 'campo': data.get('field'),
               'idioma': data.get('lang')})
    ok, error = _safe_commit('Error al borrar la traducción')
    if not ok:
        return jsonify({'error': error}), 500
    return jsonify({'ok': True}), 200
