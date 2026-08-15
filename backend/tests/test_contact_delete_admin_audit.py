"""El borrado RGPD de un contacto es solo para admin y deja rastro.

El fallo que cubre este test: `DELETE /api/v1/contacts/{id}` pedía únicamente
`get_current_user`. Cualquier usuario con rol `cliente` podía borrar de golpe
el contacto, TODAS sus conversaciones, sus mensajes y sus ficheros de audio —
irreversible y sin ninguna línea en el audit_log que dijera quién lo hizo.
La exportación de esos mismos datos sí deja rastro; el borrado, que además es
destructivo, no dejaba ninguno.

Sin BD: el 403 se resuelve en la dependencia (antes del cuerpo) y el caso de
admin usa una sesión falsa.
"""
from __future__ import annotations

import uuid

import pytest


class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj

    def scalars(self):
        return self

    def all(self):
        return []


class _FakeDb:
    def __init__(self, obj):
        self._obj = obj
        self.added: list = []
        self.commits = 0

    async def execute(self, *a, **k):
        return _FakeResult(self._obj)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


class _FakeUser:
    def __init__(self, role):
        self.id = uuid.uuid4()
        self.role = role
        self.activo = True


# ---------------------------------------------------------------------------
# Permisos
# ---------------------------------------------------------------------------


def _client_and_app():
    """App mínima con SOLO el router de contactos.

    No montamos `app.main` a propósito: al importarlo crea directorios de
    almacenamiento (/data) y arrastra media app. Aquí solo se comprueban las
    dependencias de la ruta, que son las mismas.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import contacts

    app = FastAPI()
    app.include_router(contacts.router, prefix="/api/v1")
    return TestClient(app, raise_server_exceptions=False), app


def test_un_cliente_no_puede_borrar_un_contacto():
    from app.api.deps import get_current_user
    from app.db.session import get_db
    from app.models.user import UserRole

    client, app = _client_and_app()
    operador = _FakeUser(UserRole.cliente)

    async def fake_user():
        return operador

    async def fake_db():
        yield _FakeDb(None)

    app.dependency_overrides[get_current_user] = fake_user
    app.dependency_overrides[get_db] = fake_db
    try:
        resp = client.delete(f"/api/v1/contacts/{uuid.uuid4()}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 403, (
        f"un rol 'cliente' puede borrar contacto + conversaciones + ficheros "
        f"(respuesta {resp.status_code})"
    )


def test_un_admin_si_puede_borrar_un_contacto():
    """El arreglo no puede romper el borrado legítimo."""
    from app.api.deps import get_current_user
    from app.db.session import get_db
    from app.models.user import UserRole
    from app.services import data_erasure

    client, app = _client_and_app()
    admin = _FakeUser(UserRole.admin)
    db = _FakeDb(object())

    async def fake_user():
        return admin

    async def fake_db():
        yield db

    async def fake_erase(_db, _cid):
        return {"deleted": True, "files_deleted": 2, "knowledge_gaps_deleted": 1}

    original = data_erasure.erase_contact
    data_erasure.erase_contact = fake_erase
    app.dependency_overrides[get_current_user] = fake_user
    app.dependency_overrides[get_db] = fake_db
    try:
        resp = client.delete(f"/api/v1/contacts/{uuid.uuid4()}")
    finally:
        data_erasure.erase_contact = original
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Auditoría
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_el_borrado_queda_registrado_en_auditoria(monkeypatch):
    from app.api import contacts
    from app.models.user import UserRole
    from app.services import audit, data_erasure

    admin = _FakeUser(UserRole.admin)
    db = _FakeDb(object())
    contact_id = uuid.uuid4()

    async def fake_erase(_db, cid):
        return {"deleted": True, "files_deleted": 3, "knowledge_gaps_deleted": 0}

    registros: list[dict] = []

    async def fake_record_audit(_db, **kw):
        registros.append(kw)

    monkeypatch.setattr(data_erasure, "erase_contact", fake_erase)
    monkeypatch.setattr(audit, "record_audit", fake_record_audit)

    await contacts.delete_contact(contact_id, db=db, current_user=admin)

    assert registros, "el borrado RGPD no deja ninguna línea en el audit_log"
    entry = registros[0]
    assert entry["action"] == "contact.data_erased"
    assert entry["entity"] == "contact"
    assert entry["entity_id"] == contact_id
    assert entry["user_id"] == admin.id
    # El informe del borrado (qué se llevó por delante) va en el registro.
    assert entry.get("after", {}).get("files_deleted") == 3
