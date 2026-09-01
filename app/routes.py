from __future__ import annotations
from . import app, db
from .models import (Cleaner, CleaningRecord, CareRecord, CareType,
                      ShiftType, AbsenceType)
from datetime import datetime, time as dt_time
import click


@app.teardown_appcontext
def shutdown_session(exception=None):
    if exception:
        db.session.rollback()


# ── CLI ───────────────────────────────────────────────────────────────────────

@app.cli.command('create-admin')
@click.argument('username')
def create_admin(username: str) -> None:
    """Otorga permisos de administrador a un usuario existente.

    Uso: flask create-admin <username>
    """
    cleaner = Cleaner.query.filter_by(username=username).first()
    if not cleaner:
        print(f'Usuario "{username}" no encontrado.')
        return
    cleaner.is_admin = True
    db.session.commit()
    print(f'"{username}" ahora es administrador.')


@app.cli.command('init-admin')
@click.argument('username')
@click.argument('password')
@click.option('--name', default=None, help='Nombre visible del administrador')
def init_admin(username: str, password: str, name: str | None) -> None:
    """Crea un usuario administrador. Si ya existe, actualiza su contraseña y permisos.

    Uso: flask init-admin <username> <password>
    """
    cleaner = Cleaner.query.filter_by(username=username).first()
    if cleaner:
        cleaner.set_password(password)
        cleaner.is_admin = True
        db.session.commit()
        print(f'Usuario "{username}" actualizado como administrador.')
    else:
        cleaner = Cleaner(username=username, name=name or username, is_admin=True)
        cleaner.set_password(password)
        db.session.add(cleaner)
        db.session.commit()
        print(f'Administrador "{username}" creado correctamente.')


@app.cli.command('close-orphan-sessions')
@click.option('--before', default=None, help='Fecha límite YYYY-MM-DD (cierra sesiones anteriores a esta fecha)')
@click.option('--dry-run', is_flag=True, help='Solo mostrar, no modificar')
def close_orphan_sessions(before: str | None, dry_run: bool) -> None:
    """Cierra sesiones de limpieza sin finalizar (end_time=None).

    Uso: flask close-orphan-sessions --before 2026-04-25
         flask close-orphan-sessions --dry-run
    """
    query = CleaningRecord.query.filter(CleaningRecord.end_time.is_(None))
    if before:
        cutoff = datetime.strptime(before, '%Y-%m-%d')
        query = query.filter(CleaningRecord.start_time < cutoff)
    records = query.all()
    print(f'Sesiones abiertas encontradas: {len(records)}')
    if dry_run:
        for r in records:
            print(f'  id={r.id} cleaner_id={r.cleaner_id} start={r.start_time}')
        return
    for r in records:
        db.session.delete(r)
    db.session.commit()
    print(f'{len(records)} sesiones eliminadas.')


@app.cli.command('seed-care-types')
def seed_care_types() -> None:
    """Crea los tipos de atención por defecto.

    Uso: flask seed-care-types
    """
    defaults = [
        'Aseo e higiene', 'Medicación', 'Fisioterapia',
        'Comida', 'Compañía', 'Cambio de postura', 'Cura / Heridas', 'Otro',
    ]
    created = 0
    for name in defaults:
        if not CareType.query.filter_by(name=name).first():
            db.session.add(CareType(name=name))
            created += 1
    db.session.commit()
    print(f'{created} tipo(s) de atención creados.')


@app.cli.command('auto-assign-roles')
def auto_assign_roles():
    """Assign worker roles (limpieza/atenciones/mixto) based on historical data."""
    workers = Cleaner.query.filter_by(active=True).all()
    stats = {'limpieza': 0, 'atenciones': 0, 'mixto': 0, 'sin_actividad': 0}
    for w in workers:
        cleanings = CleaningRecord.query.filter_by(cleaner_id=w.id).count()
        cares = CareRecord.query.filter_by(worker_id=w.id).count()
        if cleanings > 0 and cares > 0:
            w.role = 'mixto'
            stats['mixto'] += 1
        elif cleanings > 0:
            w.role = 'limpieza'
            stats['limpieza'] += 1
        elif cares > 0:
            w.role = 'atenciones'
            stats['atenciones'] += 1
        else:
            stats['sin_actividad'] += 1
        print(f'  {w.name:<30} limp={cleanings:<6} aten={cares:<6} -> {w.role}')
    db.session.commit()
    print(f'\nResultado: {stats}')


# ── TURNOS / CUADRANTES ─────────────────────────────────────────────────────

@app.cli.command('seed-shift-types')
def seed_shift_types():
    """Create default shift types (Mañana, Tarde, Noche)."""
    defaults = [
        {'name': 'Mañana', 'short_name': 'M', 'color': '#0d6efd', 'start_time': dt_time(7, 0), 'end_time': dt_time(15, 0), 'breaks_minutes': 30, 'sort_order': 1},
        {'name': 'Tarde', 'short_name': 'T', 'color': '#fd7e14', 'start_time': dt_time(15, 0), 'end_time': dt_time(22, 0), 'breaks_minutes': 30, 'sort_order': 2},
        {'name': 'Noche', 'short_name': 'N', 'color': '#6f42c1', 'start_time': dt_time(22, 0), 'end_time': dt_time(7, 0), 'breaks_minutes': 30, 'sort_order': 3},
    ]
    for d in defaults:
        if not ShiftType.query.filter_by(name=d['name']).first():
            db.session.add(ShiftType(**d))
            print(f'  Created: {d["name"]}')
        else:
            print(f'  Exists: {d["name"]}')
    db.session.commit()
    print('Done.')


# ─── Phase 2 & 3: Rotation Patterns, Absences, Validation ─────────────────────

@app.cli.command('seed-absence-types')
def seed_absence_types():
    defaults = [
        {'name': 'Vacaciones', 'short_name': 'VAC', 'color': '#198754', 'counts_as_worked': False},
        {'name': 'Baja médica', 'short_name': 'BAJ', 'color': '#dc3545', 'counts_as_worked': False},
        {'name': 'Asuntos propios', 'short_name': 'AP', 'color': '#ffc107', 'counts_as_worked': False},
        {'name': 'Permiso retribuido', 'short_name': 'PR', 'color': '#0dcaf0', 'counts_as_worked': True},
        {'name': 'Festivo', 'short_name': 'FES', 'color': '#6c757d', 'counts_as_worked': False},
    ]
    for d in defaults:
        if not AbsenceType.query.filter_by(name=d['name']).first():
            db.session.add(AbsenceType(**d))
            print(f'  Created: {d["name"]}')
        else:
            print(f'  Exists: {d["name"]}')
    db.session.commit()
    print('Done.')


@app.cli.command('purge-messages')
@click.option('--dry-run', is_flag=True, help='Solo mostrar lo que se borraria')
def purge_messages(dry_run: bool) -> None:
    """Aplica la politica de conservacion de la mensajeria.

    Uso: flask purge-messages [--dry-run]

    Borra filas **y ficheros**: por eso es un comando y no un DELETE en SQL, que
    dejaria el disco lleno de adjuntos huerfanos.

    Las retenciones se leen de AppSetting y se pueden cambiar sin desplegar. Los
    adjuntos duran menos que el texto porque son casi todo el volumen: una foto
    ocupa lo que diez mil mensajes.

    Pensado para el Task Scheduler del NAS, una vez por semana:
        docker exec <CONTENEDOR> flask purge-messages
    No un planificador dentro de la aplicacion: con dos workers se ejecutaria
    dos veces.
    """
    import os
    from datetime import timedelta
    from app.models import AppSetting, Message, MessageAttachment

    dias_texto = int(AppSetting.get('messaging_retention_text_days', '365'))
    dias_media = int(AppSetting.get('messaging_retention_media_days', '180'))
    ahora = datetime.now()
    corte_texto = ahora - timedelta(days=dias_texto)
    corte_media = ahora - timedelta(days=dias_media)
    carpeta_base = os.path.join(app.config['UPLOAD_FOLDER'], 'messaging')

    print(f'Conservacion: texto {dias_texto} dias, adjuntos {dias_media} dias')
    if dry_run:
        print('(simulacion: no se borra nada)')

    # 1. Adjuntos caducados: se sueltan del mensaje, que se queda como lapida.
    liberados = 0
    caducados = (MessageAttachment.query
                 .join(Message, Message.id == MessageAttachment.message_id)
                 .filter(Message.created_at < corte_media).all())
    for adj in caducados:
        for rel in (adj.file_path, adj.thumb_path):
            if not rel:
                continue
            ruta = os.path.join(app.config['UPLOAD_FOLDER'], rel)
            if os.path.exists(ruta):
                liberados += os.path.getsize(ruta)
                if not dry_run:
                    try:
                        os.remove(ruta)
                    except OSError as e:
                        print(f'  no se pudo borrar {rel}: {e}')
        if not dry_run:
            msg = db.session.get(Message, adj.message_id)
            if msg and not msg.deleted_at:
                msg.deleted_at = ahora
                msg.body = None
            db.session.delete(adj)
    print(f'Adjuntos caducados: {len(caducados)} ({liberados / 1048576:.1f} MB)')

    # 2. Mensajes caducados. Las marcas de lectura son enteros: que apunten a un
    #    id que ya no existe es inocuo, no hay nada que recalcular.
    viejos = Message.query.filter(Message.created_at < corte_texto).all()
    if not dry_run and viejos:
        ids = [m.id for m in viejos]
        MessageAttachment.query.filter(
            MessageAttachment.message_id.in_(ids)).delete(synchronize_session=False)
        Message.query.filter(Message.id.in_(ids)).delete(synchronize_session=False)
    print(f'Mensajes caducados: {len(viejos)}')

    if not dry_run:
        db.session.commit()
        # 3. Carpetas de meses que se han quedado vacias.
        for raiz, dirs, ficheros in os.walk(carpeta_base, topdown=False):
            if not dirs and not ficheros and raiz != carpeta_base:
                try:
                    os.rmdir(raiz)
                except OSError:
                    pass
        print('Hecho.')
