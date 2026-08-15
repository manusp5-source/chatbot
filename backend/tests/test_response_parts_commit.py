"""La respuesta del agente se guarda POR PARTE, nada más salir hacia el cliente.

El fallo que cubre este test (auditoría #4): la respuesta se troceaba en varias
partes, se enviaban una a una al cliente y el `commit` estaba UNA sola vez, al
final del bucle. Si la parte 2 fallaba (el proveedor devuelve 500, se cae la
red, muere el worker), la transacción se revertía entera: el cliente YA había
recibido la parte 1 pero en la base de datos no constaba. Consecuencias reales:

  - el historial que ve el agente no incluye lo que ya dijo → lo repite;
  - la operadora abre el inbox y no ve lo que el cliente sí ha recibido.

Aquí se prueba el invariante: toda parte ENTREGADA queda comprometida en la BD
aunque una parte posterior reviente. No necesita Postgres — se usa una sesión
falsa con la semántica mínima de transacción (commit fija, rollback revierte).
"""
from __future__ import annotations

import uuid

import pytest


class _FakeStore:
    """"BD" en memoria: guarda lo comprometido y sabe revertir lo pendiente."""

    def __init__(self) -> None:
        self.pending: list = []
        self.committed: list = []

    def add(self, obj) -> None:
        self.pending.append(obj)

    def commit(self) -> None:
        self.committed.extend(self.pending)
        self.pending = []

    def rollback(self) -> None:
        self.pending = []


class _FakeSession:
    def __init__(self, store: _FakeStore) -> None:
        self.store = store

    def add(self, obj) -> None:
        self.store.add(obj)

    async def commit(self) -> None:
        self.store.commit()

    async def rollback(self) -> None:
        self.store.rollback()


class _FakeConv:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.canal = type("C", (), {"value": "whatsapp"})()
        self.last_message_at = None


class _Boom(RuntimeError):
    """Fallo genérico del proveedor a mitad de la respuesta."""


@pytest.mark.asyncio
async def test_parte_entregada_sobrevive_al_fallo_de_la_siguiente():
    from app.services.conversation import deliver_response_parts

    store = _FakeStore()
    db = _FakeSession(store)
    conv = _FakeConv()
    enviadas: list[str] = []

    async def send(_conv, part: str) -> str:
        if part == "parte 2":
            raise _Boom("el proveedor devolvió 500")
        enviadas.append(part)
        return "wamid.1"

    with pytest.raises(_Boom):
        await deliver_response_parts(
            db, conv, ["parte 1", "parte 2", "parte 3"], send=send, pause=0
        )

    # El cliente recibió la parte 1: TIENE que constar en la BD.
    assert enviadas == ["parte 1"]
    assert [m.contenido for m in store.committed] == ["parte 1"]
    # Y nada a medias sin comprometer.
    assert store.pending == []


@pytest.mark.asyncio
async def test_todas_las_partes_se_comprometen_en_orden():
    from app.services.conversation import deliver_response_parts

    store = _FakeStore()
    db = _FakeSession(store)
    conv = _FakeConv()

    async def send(_conv, _part: str) -> str:
        return "wamid.x"

    delivered = await deliver_response_parts(
        db, conv, ["a", "b", "c"], send=send, pause=0
    )

    assert delivered == 3
    assert [m.contenido for m in store.committed] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_ventana_cerrada_corta_pero_conserva_lo_ya_enviado(monkeypatch):
    """Ventana de mensajería cerrada: se para, pero lo entregado queda guardado."""
    from app.services import conversation as conv_mod
    from app.services.channel_sender import MessagingWindowClosed

    async def _noop_log(**_kw):
        return None

    monkeypatch.setattr(conv_mod, "push_runtime_log", _noop_log)

    store = _FakeStore()
    db = _FakeSession(store)
    conv = _FakeConv()

    async def send(_conv, part: str) -> str:
        if part == "b":
            raise MessagingWindowClosed("ventana de 7 días cerrada")
        return ""

    delivered = await conv_mod.deliver_response_parts(
        db, conv, ["a", "b", "c"], send=send, pause=0
    )

    assert delivered == 1
    assert [m.contenido for m in store.committed] == ["a"]


@pytest.mark.asyncio
async def test_partes_vacias_se_saltan():
    from app.services.conversation import deliver_response_parts

    store = _FakeStore()
    db = _FakeSession(store)
    conv = _FakeConv()

    async def send(_conv, _part: str) -> str:
        return ""

    delivered = await deliver_response_parts(
        db, conv, ["a", "   ", ""], send=send, pause=0
    )
    assert delivered == 1
    assert [m.contenido for m in store.committed] == ["a"]
