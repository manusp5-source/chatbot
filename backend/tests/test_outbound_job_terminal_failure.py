"""Campañas que mueren en silencio y se quedan "en curso" para siempre.

El fallo que cubre este test: si la vuelta de envío falla y se agotan los
reintentos de Celery, la excepción se perdía y el job se quedaba en `running`
indefinidamente — sin estado terminal, sin motivo y sin nada en la UI que
dijera que aquello ya no se estaba enviando. Lo mismo si el mensaje de
re-encolado se pierde (Redis reiniciado): nadie vuelve a tocar el job.

Cubre las dos vías:
  - reintentos agotados → el job pasa a `failed` con su motivo (y queda log);
  - job `running` sin actividad desde hace mucho → el barrido periódico lo
    cierra en `failed`.

Sin Postgres: sesión de BD falsa que aplica los `UPDATE ... WHERE` guardados.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Update

from app.tasks import outbound_send as mod

# ---------------------------------------------------------------------------
# Sesión de BD falsa que SÍ aplica los UPDATE (es lo que estamos midiendo)
# ---------------------------------------------------------------------------


class _Row:
    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)


class _FakeResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return list(self._value)


class _FakeSession:
    """Entiende lo justo: SELECT de jobs/recipients y UPDATE sobre outbound_job."""

    def __init__(self, jobs: list[_Row], recipients: list[_Row]) -> None:
        self.jobs = jobs
        self.recipients = recipients
        self.commits = 0

    async def execute(self, stmt):
        from app.models.outbound_job import OutboundJob, OutboundJobRecipient

        if isinstance(stmt, Update):
            params = dict(stmt.compile().params)
            # WHERE id = :id_1 (los binds del WHERE llevan sufijo _N).
            target = params.get("id_1")
            expected_status = params.get("status_1")
            for job in self.jobs:
                if job.id != target:
                    continue
                # El WHERE de estado puede ser `== x` o `IN (x, y)`.
                if isinstance(expected_status, (list, tuple, set)):
                    if job.status not in expected_status:
                        continue
                elif expected_status is not None and job.status != expected_status:
                    continue
                for field in ("status", "error", "finished_at", "started_at"):
                    if field in params:
                        setattr(job, field, params[field])
            return _FakeResult(None)

        desc = stmt.column_descriptions[0]
        entity, name = desc["entity"], desc["name"]
        if entity is OutboundJob and name == "OutboundJob":
            return _FakeResult(self.jobs)
        if entity is OutboundJob and name == "status":
            return _FakeResult(self.jobs[0].status if self.jobs else None)
        if entity is OutboundJobRecipient or name == "max":
            sent = [r.sent_at for r in self.recipients if r.sent_at is not None]
            return _FakeResult(max(sent) if sent else None)
        return _FakeResult(None)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        pass


def _patch_db(monkeypatch, jobs: list[_Row], recipients: list[_Row]) -> _FakeSession:
    from contextlib import asynccontextmanager

    session = _FakeSession(jobs, recipients)

    @asynccontextmanager
    async def fake_db_session():
        yield session

    monkeypatch.setattr(mod, "db_session", fake_db_session)
    return session


def _job(**kw) -> _Row:
    from app.models.outbound_job import JOB_STATUS_RUNNING

    now = datetime.now(timezone.utc)
    base = {
        "id": uuid.uuid4(),
        "status": JOB_STATUS_RUNNING,
        "error": None,
        "started_at": None,
        "finished_at": None,
        # Un job se crea ANTES de arrancar: si el test fija started_at, el
        # created_at coherente es ese mismo instante (o anterior).
        "created_at": kw.get("started_at") or now,
        "throttle_min_seconds": 20,
        "throttle_max_seconds": 40,
    }
    base.update(kw)
    return _Row(**base)


# ---------------------------------------------------------------------------
# Reintentos agotados → estado terminal con motivo
# ---------------------------------------------------------------------------


def test_reintentos_agotados_dejan_el_job_en_failed(monkeypatch):
    """EL FALLO: la campaña se quedaba "en curso" para siempre."""
    from app.models.outbound_job import JOB_STATUS_FAILED

    job = _job()
    _patch_db(monkeypatch, [job], [])

    async def boom(_job_id):
        raise RuntimeError("YCloud devuelve 500 sin parar")

    monkeypatch.setattr(mod, "_process_one", boom)

    # retries = max_retries → Celery ya no reintenta más.
    mod.send_next_recipient.apply(
        args=[str(job.id)], retries=mod.send_next_recipient.max_retries
    )

    assert job.status == JOB_STATUS_FAILED, (
        f"el job quedó en '{job.status}': nadie se entera de que murió"
    )
    assert job.error and "YCloud" in job.error
    assert job.finished_at is not None


def test_un_fallo_transitorio_agota_los_reintentos_antes_de_cerrar(monkeypatch):
    """No se cierra la campaña al primer tropiezo: primero se reintenta."""
    from app.models.outbound_job import JOB_STATUS_FAILED

    job = _job()
    _patch_db(monkeypatch, [job], [])
    intentos: list[str] = []

    async def boom(job_id):
        intentos.append(str(job_id))
        raise RuntimeError("timeout puntual")

    monkeypatch.setattr(mod, "_process_one", boom)

    mod.send_next_recipient.apply(args=[str(job.id)], retries=0)

    # Intento original + max_retries reintentos (en modo eager encadenan).
    assert len(intentos) == mod.send_next_recipient.max_retries + 1
    # Y solo al agotarlos, cierre terminal.
    assert job.status == JOB_STATUS_FAILED


# ---------------------------------------------------------------------------
# Barrido de jobs "running" que ya no los mueve nadie
# ---------------------------------------------------------------------------


def test_stale_after_seconds_da_margen_al_ritmo_del_job():
    # Ritmo normal (40s entre envíos): media hora de gracia.
    assert mod.stale_after_seconds(40) == mod.STALE_MIN_SECONDS
    # Ritmo lentísimo (1h entre envíos): el margen escala con él.
    assert mod.stale_after_seconds(3600) == 3 * 3600


async def test_reap_cierra_los_jobs_sin_actividad(monkeypatch):
    from app.models.outbound_job import JOB_STATUS_FAILED

    now = datetime.now(timezone.utc)
    muerto = _job(started_at=now - timedelta(hours=4))
    vivo = _job(started_at=now - timedelta(minutes=2))
    _patch_db(monkeypatch, [muerto, vivo], [])

    reaped = await mod._reap_stale_jobs(now=now)

    assert reaped == 1
    assert muerto.status == JOB_STATUS_FAILED
    assert "sin actividad" in (muerto.error or "").lower()
    assert vivo.status == "running"


async def test_reap_respeta_los_jobs_con_envios_recientes(monkeypatch):
    now = datetime.now(timezone.utc)
    job = _job(started_at=now - timedelta(hours=4))
    # Arrancó hace horas pero acaba de enviar: está vivo.
    reciente = _Row(job_id=job.id, sent_at=now - timedelta(seconds=30))
    _patch_db(monkeypatch, [job], [reciente])

    assert await mod._reap_stale_jobs(now=now) == 0
    assert job.status == "running"


async def test_reap_cierra_tambien_los_que_nunca_arrancaron(monkeypatch):
    """El `queued` cuyo primer mensaje se perdió: "en cola" para siempre."""
    from app.models.outbound_job import JOB_STATUS_FAILED, JOB_STATUS_QUEUED

    now = datetime.now(timezone.utc)
    job = _job(status=JOB_STATUS_QUEUED, created_at=now - timedelta(hours=4))
    _patch_db(monkeypatch, [job], [])

    assert await mod._reap_stale_jobs(now=now) == 1
    assert job.status == JOB_STATUS_FAILED


@pytest.mark.parametrize("estado", ["done", "canceled", "failed"])
async def test_reap_no_toca_jobs_ya_terminados(monkeypatch, estado):
    now = datetime.now(timezone.utc)
    job = _job(status=estado, started_at=now - timedelta(days=3))
    _patch_db(monkeypatch, [job], [])

    # El barrido consulta SOLO los que siguen en curso; el doble devuelve todo,
    # así que esto comprueba además el guardado del propio UPDATE.
    await mod._reap_stale_jobs(now=now)

    assert job.status == estado
