"""Idempotencia del envío masivo: un reintento NO puede reenviar a quien ya recibió.

El fallo que cubre este test: el envío al proveedor ocurría dentro de una
transacción que se cerraba mucho después (contadores, recuento de pendientes,
transición a `done`). Si algo fallaba entre medias — la BD, Redis, el propio
proceso — la transacción se revertía, el destinatario volvía a quedar `pending`
y el reintento de Celery le mandaba la plantilla OTRA VEZ. En producción: el
mismo cliente recibiendo la misma campaña 2-4 veces.

No necesita Postgres. Se sustituye `db_session` por una sesión falsa que emula
lo único que importa aquí: que `commit()` fija el estado y que una excepción
revierte lo que no se comprometió.
"""
from __future__ import annotations

import contextlib
import uuid

import pytest
from sqlalchemy import Update


# ---------------------------------------------------------------------------
# Sesión de BD falsa con semántica de transacción (commit / rollback)
# ---------------------------------------------------------------------------


class _Row:
    """Fila con atributos mutables, como un objeto ORM cargado."""

    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)


class _Store:
    """"BD" en memoria: guarda el ÚLTIMO estado comprometido y sabe revertir."""

    def __init__(self, job: _Row, recipients: list[_Row]) -> None:
        self.job = job
        self.recipients = recipients
        self.updates: list[Update] = []
        self._committed = self._snapshot()

    def _snapshot(self) -> tuple[dict, list[dict]]:
        return dict(self.job.__dict__), [dict(r.__dict__) for r in self.recipients]

    def commit(self) -> None:
        self._committed = self._snapshot()

    def rollback(self) -> None:
        job, recs = self._committed
        self.job.__dict__.clear()
        self.job.__dict__.update(job)
        for row, data in zip(self.recipients, recs, strict=True):
            row.__dict__.clear()
            row.__dict__.update(data)


class _FakeResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        return self._value


class _FakeSession:
    """Sesión mínima: entiende las 4 sentencias que emite `_process_one`.

    Los `UPDATE` de contadores no se interpretan (solo se registran): lo que se
    está probando es el ORDEN de los commits respecto al envío, no la
    aritmética de `sent_ok` / `sent_error`.
    """

    def __init__(self, store: _Store, events: list[str], boom_on_count: bool) -> None:
        self.store = store
        self.events = events
        self.boom_on_count = boom_on_count

    async def execute(self, stmt):
        from app.models.outbound_job import (
            RECIPIENT_STATUS_PENDING,
            OutboundJob,
            OutboundJobRecipient,
        )
        from app.models.outbound_optout import OutboundOptOut

        if isinstance(stmt, Update):
            self.store.updates.append(stmt)
            return _FakeResult(None)

        desc = stmt.column_descriptions[0]
        entity, name = desc["entity"], desc["name"]
        # Consulta de baja permanente, ANTES del envío. En estos escenarios no
        # hay nadie de baja: lo que se prueba aquí es el orden envío/commit.
        if entity is OutboundOptOut:
            return _FakeResult(None)
        if entity is OutboundJob and name == "OutboundJob":
            return _FakeResult(self.store.job)
        if entity is OutboundJob and name == "status":
            return _FakeResult(self.store.job.status)
        if entity is OutboundJobRecipient:
            pending = [
                r for r in self.store.recipients if r.status == RECIPIENT_STATUS_PENDING
            ]
            pending.sort(key=lambda r: r.position)
            return _FakeResult(pending[0] if pending else None)
        # select(func.count()) → el recuento de pendientes, justo DESPUÉS del
        # envío. Aquí inyectamos el fallo intermedio.
        self.events.append("count")
        if self.boom_on_count:
            raise RuntimeError("se cayó la conexión con la BD tras el envío")
        return _FakeResult(
            sum(1 for r in self.store.recipients if r.status == RECIPIENT_STATUS_PENDING)
        )

    async def commit(self) -> None:
        self.events.append("commit")
        self.store.commit()

    async def rollback(self) -> None:
        self.store.rollback()


def _patch_db(monkeypatch, store: _Store, events: list[str], *, boom_on_count: bool):
    from contextlib import asynccontextmanager

    from app.tasks import outbound_send as mod

    @asynccontextmanager
    async def fake_db_session():
        session = _FakeSession(store, events, boom_on_count)
        try:
            yield session
        except Exception:
            await session.rollback()
            raise

    monkeypatch.setattr(mod, "db_session", fake_db_session)


class _FakeWA:
    def __init__(self, events: list[str]) -> None:
        self.sent: list[str] = []
        self.events = events

    async def send_template(self, phone, template_name, language, body_variables=None):
        self.sent.append(phone)
        self.events.append("send")
        return f"ext-{phone}"


def _patch_provider(monkeypatch, fake: _FakeWA) -> None:
    monkeypatch.setattr("app.providers.whatsapp.get_whatsapp_provider", lambda: fake)

    async def _not_blocked(_phone):
        return False

    monkeypatch.setattr("app.services.agent_guardrails.is_phone_blocked", _not_blocked)


def _seed(n: int = 1) -> tuple[_Store, uuid.UUID]:
    from app.models.outbound_job import (
        JOB_STATUS_RUNNING,
        RECIPIENT_STATUS_PENDING,
    )

    job_id = uuid.uuid4()
    job = _Row(
        id=job_id,
        status=JOB_STATUS_RUNNING,
        template_name="recordatorio",
        language="es",
        throttle_min_seconds=0,
        throttle_max_seconds=0,
    )
    recipients = [
        _Row(
            id=uuid.uuid4(),
            job_id=job_id,
            position=i,
            phone=f"+3460000000{i}",
            variables=[],
            status=RECIPIENT_STATUS_PENDING,
            external_id=None,
            error=None,
            sent_at=None,
        )
        for i in range(n)
    ]
    return _Store(job, recipients), job_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reintento_no_reenvia_al_destinatario_ya_enviado(monkeypatch):
    """Si algo falla DESPUÉS del envío, el reintento no debe volver a enviar.

    Un solo destinatario a propósito: así cualquier envío de la segunda vuelta
    es forzosamente un DUPLICADO.
    """
    from app.tasks.outbound_send import _process_one

    events: list[str] = []
    store, job_id = _seed(1)
    fake = _FakeWA(events)
    _patch_provider(monkeypatch, fake)
    _patch_db(monkeypatch, store, events, boom_on_count=True)

    # Dos vueltas: la original y el reintento de Celery tras el error.
    for _ in range(2):
        with contextlib.suppress(RuntimeError):
            await _process_one(job_id)

    assert fake.sent == ["+34600000000"], (
        f"el destinatario recibió el mensaje {len(fake.sent)} veces: {fake.sent}"
    )
    assert store.recipients[0].status == "ok"
    assert store.recipients[0].external_id == "ext-+34600000000"


@pytest.mark.asyncio
async def test_el_envio_se_comprometa_antes_de_seguir(monkeypatch):
    """El commit del destinatario va INMEDIATAMENTE después del envío."""
    from app.tasks.outbound_send import _process_one

    events: list[str] = []
    store, job_id = _seed(2)
    fake = _FakeWA(events)
    _patch_provider(monkeypatch, fake)
    _patch_db(monkeypatch, store, events, boom_on_count=False)

    await _process_one(job_id)

    assert events[:2] == ["send", "commit"], (
        f"entre el envío y su commit pasan otras operaciones: {events}"
    )


@pytest.mark.asyncio
async def test_reintento_tras_fallo_de_envio_tampoco_reintenta_ese_destinatario(
    monkeypatch,
):
    """Un envío fallido también queda registrado: no se reintenta en la vuelta
    siguiente (el comportamiento de siempre: el error se ve en el progreso)."""
    from app.tasks.outbound_send import _process_one

    events: list[str] = []
    store, job_id = _seed()

    class _FailingWA(_FakeWA):
        async def send_template(self, phone, *a, **kw):
            self.sent.append(phone)
            self.events.append("send")
            raise RuntimeError("YCloud rechazó el mensaje")

    fake = _FailingWA(events)
    _patch_provider(monkeypatch, fake)
    _patch_db(monkeypatch, store, events, boom_on_count=True)

    for _ in range(2):
        with contextlib.suppress(RuntimeError):
            await _process_one(job_id)

    assert fake.sent == ["+34600000000"]
    assert store.recipients[0].status == "error"
