"""Una cuenta desactivada no debe poder entrar por ninguna de las dos puertas.

`active=False` significa que la persona ya no trabaja aquí. Hasta ahora ninguno
de los dos logins lo comprobaba: la cuenta seguía entrando y, en el caso de la
webapp, se llevaba un token válido durante 7 días.
"""
import pytest

from app.models import Cleaner


@pytest.fixture
def inactive_worker(db):
    user = Cleaner(username='exlimpiadora', name='Ex Limpiadora',
                   is_admin=False, active=False)
    user.set_password('secreta123')
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def inactive_admin(db):
    user = Cleaner(username='exadmin', name='Ex Admin',
                   is_admin=True, active=False)
    user.set_password('secreta123')
    db.session.add(user)
    db.session.commit()
    return user


# ── Webapp de trabajadoras (JWT) ─────────────────────────────────────────────

def test_trabajadora_desactivada_no_obtiene_token(client, inactive_worker):
    res = client.post('/login', json={'username': 'exlimpiadora', 'password': 'secreta123'})

    assert res.status_code == 403
    assert 'access_token' not in res.get_json()


def test_trabajadora_desactivada_recibe_un_mensaje_util(client, inactive_worker):
    """No es 'credenciales incorrectas': la contraseña es correcta, la cuenta no."""
    res = client.post('/login', json={'username': 'exlimpiadora', 'password': 'secreta123'})

    assert 'desactivada' in res.get_json()['error'].lower()


def test_contrasena_incorrecta_no_revela_que_la_cuenta_existe(client, inactive_worker):
    """El estado de la cuenta solo se revela tras acertar la contraseña."""
    res = client.post('/login', json={'username': 'exlimpiadora', 'password': 'mal'})

    assert res.status_code == 401
    assert 'desactivada' not in res.get_json()['error'].lower()


def test_trabajadora_activa_sigue_entrando(client, cleaner_user):
    res = client.post('/login', json={'username': 'limpiadora1', 'password': 'limpia123'})

    assert res.status_code == 200
    assert res.get_json()['access_token']


# ── Panel de administración (sesión) ─────────────────────────────────────────

def test_admin_desactivado_no_inicia_sesion(client, inactive_admin):
    res = client.post('/admin/login',
                      data={'username': 'exadmin', 'password': 'secreta123'},
                      follow_redirects=True)

    assert res.status_code == 200
    # Sigue en el login, no ha entrado al panel
    res2 = client.get('/manage-residents')
    assert res2.status_code in (302, 401, 403)


def test_admin_activo_sigue_entrando(client, admin_user):
    client.post('/admin/login',
                data={'username': 'admin', 'password': 'admin123'},
                follow_redirects=True)

    res = client.get('/manage-residents')
    assert res.status_code == 200
