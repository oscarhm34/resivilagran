"""
test_seguridad_acceso.py — Contrasenas y rastro de quien entra.

Dos cosas que hasta ahora no existian y que importan por motivos distintos.

La **politica de contrasenas**: el formulario de alta aceptaba una contrasena
vacia y creaba la cuenta igual, con el hash de la cadena vacia. Solo longitud y
una lista corta de prohibidas; nada de exigir simbolos, porque quien usa esto
teclea en un movil y la complejidad obligatoria no produce contrasenas mejores,
produce contrasenas apuntadas en un papel.

El **registro de autenticacion**: no quedaba constancia de ningun login, ni
correcto ni fallido, ni de ningun logout. Despues de un incidente eso significa
no poder decir por donde entraron ni si siguen dentro, y el RGPD da 72 horas
para describir el alcance de una brecha. Lo que se vigila aqui es que la
entrada se escriba, que distinga los casos, y sobre todo que **nunca** lleve la
contrasena probada.
"""

import pytest

from app.models import AuditLog, Cleaner


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def trabajadora(db):
    c = Cleaner(username='ana', name='Ana Pons', is_admin=False, active=True,
                role='atenciones')
    c.set_password('contrasenaLarga2026')
    db.session.add(c)
    db.session.commit()
    return c


def _entradas(accion=None):
    q = AuditLog.query.filter_by(table_name='auth')
    if accion:
        q = q.filter_by(action=accion)
    return q.all()


# ── La politica de contrasenas ────────────────────────────────────────────────

@pytest.mark.parametrize('password, motivo', [
    ('', 'vacia'),
    ('corta', 'menos de 10'),
    ('123456789', 'nueve caracteres'),
    ('password', 'prohibida'),
    ('lavilagran', 'prohibida'),
])
def test_una_contrasena_floja_se_rechaza(app, password, motivo):
    from app.utils import validar_contrasena
    assert validar_contrasena(password) is not None, motivo


def test_una_contrasena_razonable_se_acepta(app):
    from app.utils import validar_contrasena
    assert validar_contrasena('martes-de-octubre') is None
    # Exactamente el minimo: el limite entra.
    assert validar_contrasena('gatoVerde1') is None
    assert len('gatoVerde1') == 10


def test_la_contrasena_no_puede_llevar_el_usuario_dentro(app):
    from app.utils import validar_contrasena
    # La comprobacion no distingue mayusculas: 'anaPons' contiene 'anapons'.
    assert validar_contrasena('anaPonsSegura', 'anapons') is not None
    assert validar_contrasena('maria12345678', 'maria') is not None
    assert validar_contrasena('jueves-de-marzo', 'maria') is None


def test_no_se_crea_una_trabajadora_sin_contrasena(auth_client, db):
    """Antes esto creaba la cuenta con el hash de la cadena vacia."""
    auth_client.post('/cleaners/add_edit', data={
        'username': 'nueva', 'name': 'Nueva', 'role': 'atenciones',
        'active': 'on', 'password': '',
    }, follow_redirects=True)
    assert Cleaner.query.filter_by(username='nueva').first() is None


def test_no_se_crea_una_trabajadora_con_contrasena_corta(auth_client, db):
    res = auth_client.post('/cleaners/add_edit', data={
        'username': 'nueva', 'name': 'Nueva', 'role': 'atenciones',
        'active': 'on', 'password': 'corta',
    }, follow_redirects=True)
    assert 'al menos' in res.get_data(as_text=True)
    assert Cleaner.query.filter_by(username='nueva').first() is None


def test_con_una_contrasena_valida_si_se_crea(auth_client, db):
    auth_client.post('/cleaners/add_edit', data={
        'username': 'nueva', 'name': 'Nueva', 'role': 'atenciones',
        'active': 'on', 'password': 'jueves-de-marzo',
    }, follow_redirects=True)
    nueva = Cleaner.query.filter_by(username='nueva').first()
    assert nueva is not None
    assert nueva.check_password('jueves-de-marzo')


def test_editar_sin_tocar_la_contrasena_sigue_funcionando(auth_client, db,
                                                          trabajadora):
    """Dejarla vacia al editar significa 'no la cambies', y debe seguir asi."""
    hash_antes = trabajadora.password_hash
    auth_client.post('/cleaners/add_edit', data={
        'cleaner_id': trabajadora.id, 'username': 'ana', 'name': 'Ana Pons',
        'role': 'atenciones', 'active': 'on', 'password': '',
    }, follow_redirects=True)

    db.session.expire_all()
    assert db.session.get(Cleaner, trabajadora.id).password_hash == hash_antes


def test_al_editar_una_contrasena_nueva_tambien_se_valida(auth_client, db,
                                                          trabajadora):
    hash_antes = trabajadora.password_hash
    auth_client.post('/cleaners/add_edit', data={
        'cleaner_id': trabajadora.id, 'username': 'ana', 'name': 'Ana Pons',
        'role': 'atenciones', 'active': 'on', 'password': 'abc',
    }, follow_redirects=True)

    db.session.expire_all()
    assert db.session.get(Cleaner, trabajadora.id).password_hash == hash_antes


# ── El rastro de quien entra (panel) ──────────────────────────────────────────

def test_un_login_correcto_queda_registrado(client, db, admin_user):
    client.post('/admin/login', data={'username': 'admin',
                                      'password': 'admin123'})
    entradas = _entradas('login')
    assert len(entradas) == 1
    assert entradas[0].user_id == admin_user.id


def test_un_login_fallido_queda_registrado_sin_la_contrasena(client, db,
                                                             admin_user):
    """La contrasena probada no se guarda: ni entera, ni en trozos, ni su largo."""
    client.post('/admin/login', data={'username': 'admin',
                                      'password': 'ContrasenaInventada77'})
    entradas = _entradas('login_fallido')
    assert len(entradas) == 1
    assert entradas[0].user_id == admin_user.id
    assert 'ContrasenaInventada77' not in (entradas[0].details or '')
    assert 'contrasena incorrecta' in (entradas[0].details or '')


def test_un_usuario_que_no_existe_queda_registrado_sin_autor(client, db):
    client.post('/admin/login', data={'username': 'fantasma',
                                      'password': 'loquesea1234'})
    entradas = _entradas('login_fallido')
    assert len(entradas) == 1
    assert entradas[0].user_id is None
    assert 'fantasma' in (entradas[0].details or '')


def test_una_trabajadora_sin_permisos_de_admin_queda_distinguida(client, db,
                                                                 trabajadora):
    """Al usuario se le dice lo mismo siempre; en la auditoria se distingue."""
    client.post('/admin/login', data={'username': 'ana',
                                      'password': 'contrasenaLarga2026'})
    entradas = _entradas('login_fallido')
    assert len(entradas) == 1
    assert 'sin permisos' in (entradas[0].details or '')


def test_cerrar_sesion_queda_registrado(auth_client, db, admin_user):
    auth_client.post('/admin/logout')
    entradas = _entradas('logout')
    assert len(entradas) == 1
    assert entradas[0].user_id == admin_user.id


# ── El rastro de quien entra (PWA) ────────────────────────────────────────────

def test_el_login_de_la_pwa_queda_registrado(client, db, trabajadora):
    res = client.post('/login', json={'username': 'ana',
                                      'password': 'contrasenaLarga2026'})
    assert res.status_code == 200
    entradas = _entradas('login')
    assert len(entradas) == 1
    assert entradas[0].user_id == trabajadora.id


def test_el_login_fallido_de_la_pwa_queda_registrado(client, db, trabajadora):
    client.post('/login', json={'username': 'ana', 'password': 'noEsEsta123'})
    entradas = _entradas('login_fallido')
    assert len(entradas) == 1
    assert 'noEsEsta123' not in (entradas[0].details or '')


def test_una_cuenta_desactivada_queda_registrada_con_su_motivo(client, db,
                                                               trabajadora):
    trabajadora.active = False
    db.session.commit()

    res = client.post('/login', json={'username': 'ana',
                                      'password': 'contrasenaLarga2026'})
    assert res.status_code == 403
    entradas = _entradas('login_fallido')
    assert len(entradas) == 1
    assert 'desactivada' in (entradas[0].details or '')


def test_el_registro_de_autenticacion_sobrevive_a_un_rollback(client, db,
                                                              admin_user):
    """Hace su propio commit: es la traza que no se puede perder."""
    client.post('/admin/login', data={'username': 'admin',
                                      'password': 'admin123'})
    db.session.rollback()
    assert len(_entradas('login')) == 1


# ── Que la auditoria sepa quien hizo las cosas ────────────────────────────────

def test_log_audit_fuera_de_una_peticion_no_revienta(app, db):
    """Tambien se llama desde comandos de mantenimiento, sin peticion."""
    from app.utils import log_audit
    with app.app_context():
        log_audit('update', 'prueba', 1, {'campo': 'valor'})
        db.session.commit()
    entrada = AuditLog.query.filter_by(table_name='prueba').one()
    assert entrada.user_id is None
    assert entrada.ip_address is None


def test_la_auditoria_de_una_accion_del_panel_lleva_autor(auth_client, db,
                                                          admin_user,
                                                          trabajadora):
    auth_client.post('/cleaners/add_edit', data={
        'cleaner_id': trabajadora.id, 'username': 'ana', 'name': 'Ana Pons',
        'role': 'atenciones', 'active': 'on', 'password': '',
    }, follow_redirects=True)
    entrada = AuditLog.query.filter_by(table_name='cleaner',
                                       action='update').first()
    assert entrada is not None
    assert entrada.user_id == admin_user.id


def test_el_espejo_de_la_auditoria_no_lleva_datos_de_residentes(app, db, caplog):
    """Al registro va que campos se tocaron, nunca lo que valen."""
    import logging
    from app.utils import log_audit

    with caplog.at_level(logging.INFO):
        with app.app_context():
            log_audit('update', 'resident', 7,
                      {'nombre': 'Dolores Garcia', 'alergias': 'penicilina'})
    texto = caplog.text
    assert 'AUDIT' in texto
    assert 'nombre' in texto          # las claves si
    assert 'Dolores Garcia' not in texto
    assert 'penicilina' not in texto
