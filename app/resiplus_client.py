"""Cliente SOAP para la API de Resiplus."""
from __future__ import annotations

from zeep import Client
from zeep.transports import Transport
from requests import Session

RESIPLUS_BASE_URL = "http://desktop-erjegjt:7777/Services"


def _get_client(service_name: str) -> Client:
    """Crea un cliente zeep para el servicio indicado."""
    wsdl_url = f"{RESIPLUS_BASE_URL}/{service_name}.svc?singleWsdl"
    session = Session()
    session.timeout = 10
    transport = Transport(session=session)
    return Client(wsdl_url, transport=transport)


def login(username: str, password: str, close_existing: bool = True) -> str | None:
    """Autentica contra Resiplus y devuelve el sessionGuid, o None si falla."""
    client = _get_client("LogInService")
    try:
        result = client.service.AreUserAndPasswordCorrect(
            userName=username,
            password=password,
            closeExistingSessions=close_existing,
            extension="NFC_CleaningApp"
        )
        return result
    except Exception as e:
        print(f"[Resiplus] Error de login: {e}")
        return None


def logout(session_guid: str) -> bool:
    """Cierra la sesion en Resiplus."""
    client = _get_client("LogInService")
    try:
        client.service.LogOut(sessionGuid=session_guid)
        return True
    except Exception as e:
        print(f"[Resiplus] Error de logout: {e}")
        return False
