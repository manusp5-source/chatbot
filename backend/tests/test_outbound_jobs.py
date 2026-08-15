"""Tests del envío masivo en segundo plano (jobs + tarea Celery).

Cobertura:
  - create_outbound_job: crea job + filas de destinatario, deduplica por
    teléfono, recorta la ventana de ritmo y respeta el tope.
  - _process_one (tarea): procesa los destinatarios uno a uno con un provider
    SIMULADO, actualiza contadores y termina en `done`.
  - un fallo de envío se registra (sent_error) y NO detiene el job.
  - cancelar a mitad detiene los envíos y deja los pendientes sin tocar.
  - el wrapper síncrono se re-encola con `countdown` (sin DB).

Patrón DB-gated idéntico a test_template_vars.py: los endpoints/tarea se llaman
directamente como funciones inyectando un AsyncSession real. El provider de
WhatsApp y el `.delay()` de Celery se monkeypatchean para no tocar red/broker.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest


# ---------------------------------------------------------------------------
# DB-gate (mismo patrón que test_template_vars.py)
# ---------------------------------------------------------------------------


def _db_available() -> bool:
    try:
        from sqlalchemy import text

        from app.db.session import db_session

        async def _check():
            async with db_session() as db:
                await db.execute(text("select 1"))

        asyncio.run(_check())
        return True
    except Exception:
        return False


pytestmark_db = pytest.mark.skipif(
    not _db_available(),
    reason="DB no disponible en este entorno (se ejecuta en CI con Postgres)",
)


# ---------------------------------------------------------------------------
# Provider de WhatsApp simulado
# ---------------------------------------------------------------------------


class _FakeWA:
    """Provider WhatsApp falso: registra los envíos y devuelve un id sintético.

    `fail_phones` permite simular fallos de YCloud para ciertos teléfonos.
    """

    def __init__(self, fail_phones: set[str] | None = None):
        self.sent: list[tuple[str, list[str]]] = []
        self.fail_phones = fail_phones or set()

    async def send_template(self, phone, template_name, language, body_variables=None):
        if phone in self.fail_phones:
            raise RuntimeError("YCloud rechazó el mensaje")
        self.sent.append((phone, list(body_variables or [])))
        return f"ext-{phone}"


def _patch_provider(monkeypatch, fake: _FakeWA) -> None:
    monkeypatch.setattr("app.providers.whatsapp.get_whatsapp_provider", lambda: fake)


def _patch_no_enqueue(monkeypatch) -> None:
    """Evita que create_outbound_job toque el broker Celery (.delay).

    Y da por buenas las comprobaciones previas (credenciales + worker vivo):
    dependen de que haya un worker de verdad escuchando en el Redis de la
    máquina, así que sin esto el resultado del test cambiaría según quién lo
    ejecute. Esas dos puertas tienen sus propios tests en
    test_outbound_difusion.py.
    """
    from app.api import admin as admin_mod
    from app.tasks import outbound_send as mod

    monkeypatch.setattr(mod.send_next_recipient, "delay", lambda *a, **k: None)

    async def _sin_problema():
        return None

    monkeypatch.setattr(admin_mod, "_worker_problem", _sin_problema)
    monkeypatch.setattr(admin_mod, "_credentials_problem", _sin_problema)


# ---------------------------------------------------------------------------
# Helpers de siembra (idénticos en espíritu a test_template_vars.py)
# ---------------------------------------------------------------------------


async def _seed_user():
    from app.db.session import db_session
    from app.models.user import User, UserRole

    async with db_session() as db:
        user = User(
            email=f"user-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="x",
            role=UserRole.admin,
            nombre="Operador Test",
        )
        db.add(user)
        await db.flush()
        uid = user.id
        await db.commit()
        return uid


async def _get_user(uid):
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.user import User

    async with db_session() as db:
        return (await db.execute(select(User).where(User.id == uid))).scalar_one()


async def _recipients_of(job_id):
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.outbound_job import OutboundJobRecipient

    async with db_session() as db:
        return (
            await db.execute(
                select(OutboundJobRecipient)
                .where(OutboundJobRecipient.job_id == job_id)
                .order_by(OutboundJobRecipient.position)
            )
        ).scalars().all()


async def _get_job(job_id):
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.outbound_job import OutboundJob

    async with db_session() as db:
        return (
            await db.execute(select(OutboundJob).where(OutboundJob.id == job_id))
        ).scalar_one()


def _body(**kw):
    from app.api.admin import OutboundJobCreate, OutboundJobRecipientIn

    recipients = [OutboundJobRecipientIn(**r) for r in kw.pop("recipients")]
    return OutboundJobCreate(recipients=recipients, **kw)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytestmark_db
@pytest.mark.asyncio
async def test_create_job_dedupes_and_clamps_throttle(monkeypatch):
    from app.api.admin import create_outbound_job
    from app.db.session import db_session

    _patch_provider(monkeypatch, _FakeWA())
    _patch_no_enqueue(monkeypatch)
    user = await _get_user(await _seed_user())
    p = f"+34{uuid.uuid4().int % 10**9:09d}"

    body = _body(
        template_name="recordatorio",
        language="es",
        throttle_min_seconds=50,
        throttle_max_seconds=10,  # max < min → se normaliza a >= min
        recipients=[
            {"phone": p, "variables": ["Ana"]},
            {"phone": p, "variables": ["Duplicado"]},  # mismo teléfono → fuera
            {"phone": "", "variables": []},            # vacío → fuera
        ],
    )

    async with db_session() as db:
        out = await create_outbound_job(body, db=db, user=user)

    assert out.status == "queued"
    assert out.total == 1  # dedup + descarta vacío
    assert out.throttle_min_seconds == 50
    assert out.throttle_max_seconds == 50  # max normalizado a >= min

    recipients = await _recipients_of(out.id)
    assert len(recipients) == 1
    assert recipients[0].phone == p
    assert recipients[0].variables == ["Ana"]
    assert recipients[0].status == "pending"


@pytestmark_db
@pytest.mark.asyncio
async def test_process_sends_all_and_marks_done(monkeypatch):
    from app.api.admin import create_outbound_job
    from app.db.session import db_session
    from app.tasks.outbound_send import _process_one

    fake = _FakeWA()
    _patch_provider(monkeypatch, fake)
    _patch_no_enqueue(monkeypatch)
    user = await _get_user(await _seed_user())
    phones = [f"+34{uuid.uuid4().int % 10**9:09d}" for _ in range(3)]

    body = _body(
        template_name="recordatorio",
        language="es",
        throttle_min_seconds=0,
        throttle_max_seconds=0,
        recipients=[{"phone": ph, "variables": [f"v{i}"]} for i, ph in enumerate(phones)],
    )
    async with db_session() as db:
        out = await create_outbound_job(body, db=db, user=user)

    # Procesa hasta agotar (simulando el re-encolado del worker).
    guard = 0
    while True:
        delay = await _process_one(out.id)
        guard += 1
        if delay is None or guard > 10:
            break

    job = await _get_job(out.id)
    assert job.status == "done"
    assert job.sent_ok == 3
    assert job.sent_error == 0
    assert job.started_at is not None
    assert job.finished_at is not None
    assert len(fake.sent) == 3
    assert all(r.status == "ok" and r.external_id for r in await _recipients_of(out.id))


@pytestmark_db
@pytest.mark.asyncio
async def test_failed_recipient_counts_as_error_and_continues(monkeypatch):
    from app.api.admin import create_outbound_job
    from app.db.session import db_session
    from app.tasks.outbound_send import _process_one

    phones = [f"+34{uuid.uuid4().int % 10**9:09d}" for _ in range(3)]
    fake = _FakeWA(fail_phones={phones[1]})  # el del medio falla
    _patch_provider(monkeypatch, fake)
    _patch_no_enqueue(monkeypatch)
    user = await _get_user(await _seed_user())

    body = _body(
        template_name="recordatorio",
        language="es",
        throttle_min_seconds=0,
        throttle_max_seconds=0,
        recipients=[{"phone": ph, "variables": []} for ph in phones],
    )
    async with db_session() as db:
        out = await create_outbound_job(body, db=db, user=user)

    guard = 0
    while True:
        delay = await _process_one(out.id)
        guard += 1
        if delay is None or guard > 10:
            break

    job = await _get_job(out.id)
    assert job.status == "done"
    assert job.sent_ok == 2
    assert job.sent_error == 1
    recs = {r.phone: r for r in await _recipients_of(out.id)}
    assert recs[phones[1]].status == "error"
    assert recs[phones[1]].error


@pytestmark_db
@pytest.mark.asyncio
async def test_cancel_stops_sending(monkeypatch):
    from app.api.admin import cancel_outbound_job, create_outbound_job
    from app.db.session import db_session
    from app.tasks.outbound_send import _process_one

    fake = _FakeWA()
    _patch_provider(monkeypatch, fake)
    _patch_no_enqueue(monkeypatch)
    user = await _get_user(await _seed_user())
    phones = [f"+34{uuid.uuid4().int % 10**9:09d}" for _ in range(3)]

    body = _body(
        template_name="recordatorio",
        language="es",
        throttle_min_seconds=0,
        throttle_max_seconds=0,
        recipients=[{"phone": ph, "variables": []} for ph in phones],
    )
    async with db_session() as db:
        out = await create_outbound_job(body, db=db, user=user)

    # Envía el primero.
    await _process_one(out.id)
    assert len(fake.sent) == 1

    # Cancela.
    async with db_session() as db:
        canceled = await cancel_outbound_job(out.id, db=db, user=user)
    assert canceled.status == "canceled"

    # Siguiente vuelta: no debe enviar nada más.
    delay = await _process_one(out.id)
    assert delay is None
    assert len(fake.sent) == 1  # sigue siendo 1

    pending = [r for r in await _recipients_of(out.id) if r.status == "pending"]
    assert len(pending) == 2  # los 2 restantes quedan intactos


def test_wrapper_reenqueues_with_countdown(monkeypatch):
    """El wrapper síncrono re-encola con el countdown que devuelve _process_one.

    No necesita DB: monkeypatcheamos _process_one y apply_async.
    """
    from app.tasks import outbound_send as mod

    async def fake_process(job_id):
        return 7.5

    calls: list[dict] = []
    job_id = str(uuid.uuid4())
    monkeypatch.setattr(mod, "_process_one", fake_process)
    monkeypatch.setattr(
        mod.send_next_recipient, "apply_async", lambda *a, **k: calls.append(k)
    )

    # Ejecuta el cuerpo de la tarea directamente (eager, sin broker).
    mod.send_next_recipient(job_id)

    assert len(calls) == 1
    assert calls[0]["countdown"] == 7.5
    assert calls[0]["args"] == [job_id]


def test_wrapper_no_reenqueue_when_done(monkeypatch):
    """Si _process_one devuelve None (terminado/cancelado), no re-encola."""
    from app.tasks import outbound_send as mod

    async def fake_process(job_id):
        return None

    calls: list[dict] = []
    monkeypatch.setattr(mod, "_process_one", fake_process)
    monkeypatch.setattr(
        mod.send_next_recipient, "apply_async", lambda *a, **k: calls.append(k)
    )

    mod.send_next_recipient(str(uuid.uuid4()))
    assert calls == []
