"""Tests del servicio de copias (app/services/backups.py).

Cobertura de lo añadido en frecuencias + destino por copia:
  - day_slot_due (pura): frecuencias de días (diaria/semanal/mensual) en hora
    de pared Madrid — puerta de hora, intervalo de N días, fail-safe con
    frecuencias desconocidas.
  - compute_next_run: próxima ejecución semanal/mensual (última + N días a la
    hora configurada), slot pasado → ya, y rama por-horas intacta.
  - compute_retention_deletions con frecuencia semanal/mensual: conserva las
    últimas 7 copias (7 días-con-copia + 4 semanas ⊂ ellos).
  - process_dump: registra el DESTINO real de cada copia (local_file +
    upload_status): sin bucket → not_configured; subida OK → uploaded (blob
    cifrado EKB1 que descifra al original); subida fallida → failed + error
    y relanza (la task alerta).
  - Selector de destino (cloud/server/both): resolve_destination (pura, NULL
    → comportamiento histórico); process_dump con server no toca el bucket
    (skipped); con cloud borra el dump local SOLO tras subida OK y lo
    conserva si la subida falla o el bucket no está configurado.
  - DB-gated (mismo patrón que test_contact_notes.py): update_settings acepta
    weekly/monthly y rechaza valores inventados; claim_scheduled_slot sella el
    slot semanal (y no duplica); record_run persiste local_file/upload_status.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services import backups as svc

TZ = ZoneInfo("Europe/Madrid")


def madrid(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=TZ)


# ---------------------------------------------------------------------------
# day_slot_due — función pura de "¿toca copia?" para frecuencias de días
# ---------------------------------------------------------------------------


def test_day_slot_due_diaria_espera_a_la_hora_configurada():
    assert svc.day_slot_due("daily", 4, None, madrid(2026, 7, 16, 3, 50)) is False
    assert svc.day_slot_due("daily", 4, None, madrid(2026, 7, 16, 4, 0)) is True


def test_day_slot_due_diaria_no_repite_el_mismo_dia():
    last = madrid(2026, 7, 16, 4, 5)
    assert svc.day_slot_due("daily", 4, last, madrid(2026, 7, 16, 23, 0)) is False
    assert svc.day_slot_due("daily", 4, last, madrid(2026, 7, 17, 4, 0)) is True


def test_day_slot_due_semanal_espera_7_dias():
    last = madrid(2026, 7, 10, 4, 2)
    # A los 6 días NO toca aunque la hora haya pasado.
    assert svc.day_slot_due("weekly", 4, last, madrid(2026, 7, 16, 12, 0)) is False
    # A los 7 días toca — pero solo a partir de la hora configurada.
    assert svc.day_slot_due("weekly", 4, last, madrid(2026, 7, 17, 3, 0)) is False
    assert svc.day_slot_due("weekly", 4, last, madrid(2026, 7, 17, 4, 0)) is True


def test_day_slot_due_mensual_espera_30_dias():
    last = madrid(2026, 6, 16, 4, 1)
    assert svc.day_slot_due("monthly", 4, last, madrid(2026, 7, 15, 12, 0)) is False
    assert svc.day_slot_due("monthly", 4, last, madrid(2026, 7, 16, 4, 0)) is True


def test_day_slot_due_sin_ultima_copia_toca_en_cuanto_pasa_la_hora():
    assert svc.day_slot_due("weekly", 4, None, madrid(2026, 7, 16, 4, 0)) is True
    assert svc.day_slot_due("monthly", 4, None, madrid(2026, 7, 16, 4, 0)) is True


def test_day_slot_due_frecuencia_desconocida_cae_a_diaria():
    last = madrid(2026, 7, 15, 4, 0)
    assert svc.day_slot_due("cada-luna-llena", 4, last, madrid(2026, 7, 16, 4, 0)) is True


def test_day_slot_due_acepta_now_en_utc():
    # El sello se guarda en UTC: la comparación debe ser en fecha Madrid.
    last = madrid(2026, 7, 10, 4, 0).astimezone(timezone.utc)
    now = madrid(2026, 7, 17, 5, 0).astimezone(timezone.utc)
    assert svc.day_slot_due("weekly", 4, last, now) is True


# ---------------------------------------------------------------------------
# compute_next_run — semanal/mensual (hora de pared Madrid)
# ---------------------------------------------------------------------------


def test_compute_next_run_semanal_ultima_mas_7_dias():
    last = madrid(2026, 7, 10, 4, 3).astimezone(timezone.utc)
    now = madrid(2026, 7, 12, 10, 0).astimezone(timezone.utc)
    next_run = svc.compute_next_run("weekly", 4, last, now=now)
    assert next_run.astimezone(TZ) == madrid(2026, 7, 17, 4, 0)


def test_compute_next_run_mensual_ultima_mas_30_dias():
    last = madrid(2026, 6, 16, 4, 0).astimezone(timezone.utc)
    now = madrid(2026, 6, 20, 9, 0).astimezone(timezone.utc)
    next_run = svc.compute_next_run("monthly", 4, last, now=now)
    assert next_run.astimezone(TZ) == madrid(2026, 7, 16, 4, 0)


def test_compute_next_run_slot_pasado_devuelve_ya():
    # La semanal quedó atrás (servidor caído): el tick la disparará ya.
    last = madrid(2026, 7, 1, 4, 0).astimezone(timezone.utc)
    now = madrid(2026, 7, 16, 12, 0).astimezone(timezone.utc)
    assert svc.compute_next_run("weekly", 4, last, now=now) == now


def test_compute_next_run_sin_ultima_antes_de_la_hora_apunta_a_hoy():
    now = madrid(2026, 7, 16, 2, 0).astimezone(timezone.utc)
    next_run = svc.compute_next_run("weekly", 4, None, now=now)
    assert next_run.astimezone(TZ) == madrid(2026, 7, 16, 4, 0)


def test_compute_next_run_diaria_sigue_siendo_al_dia_siguiente():
    last = madrid(2026, 7, 16, 4, 2).astimezone(timezone.utc)
    now = madrid(2026, 7, 16, 15, 0).astimezone(timezone.utc)
    next_run = svc.compute_next_run("daily", 4, last, now=now)
    assert next_run.astimezone(TZ) == madrid(2026, 7, 17, 4, 0)


def test_compute_next_run_por_horas_intacta():
    now = datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc)
    last = now - timedelta(hours=2)
    assert svc.compute_next_run("6h", 4, last, now=now) == last + timedelta(hours=6)


# ---------------------------------------------------------------------------
# Retención remota con frecuencias semanal/mensual
# ---------------------------------------------------------------------------


def _keys_cada_n_dias(n: int, count: int) -> list[str]:
    base = madrid(2026, 7, 16, 4, 0)
    return [
        f"{svc.APP_PREFIX}{(base - timedelta(days=n * i)).strftime(svc.KEY_STAMP_FMT)}"
        f"{svc.KEY_SUFFIX}"
        for i in range(count)
    ]


def test_retencion_semanal_conserva_las_ultimas_7():
    keys = _keys_cada_n_dias(7, 10)
    to_delete = svc.compute_retention_deletions(keys, "weekly")
    # 7 días-con-copia (las 4 semanas ISO más recientes caen dentro): quedan 7.
    assert sorted(to_delete) == sorted(keys[7:])


def test_retencion_mensual_conserva_las_ultimas_7():
    keys = _keys_cada_n_dias(30, 10)
    to_delete = svc.compute_retention_deletions(keys, "monthly")
    assert sorted(to_delete) == sorted(keys[7:])


# ---------------------------------------------------------------------------
# process_dump — registro del DESTINO de cada copia
# ---------------------------------------------------------------------------


@pytest.fixture
def registro(monkeypatch) -> list[dict]:
    """Captura los record_run sin tocar la BD."""
    records: list[dict] = []

    async def fake_record(**kwargs):
        records.append(kwargs)

    monkeypatch.setattr(svc, "record_run", fake_record)
    return records


@pytest.fixture
def dump_file(tmp_path):
    path = tmp_path / "chatbot_20260716_040000.dump"
    path.write_bytes(b"PGDMP" + b"x" * 2048)
    return path


async def test_process_dump_sin_bucket_registra_solo_servidor(
    monkeypatch, registro, dump_file
):
    async def sin_cfg():
        return None

    monkeypatch.setattr(svc, "get_s3_config", sin_cfg)

    await svc.process_dump(dump_file, kind="manual", dump_duration_ms=10)

    assert len(registro) == 1
    r = registro[0]
    assert r["status"] == "ok"
    assert r["upload_status"] == "not_configured"
    assert r["local_file"] == dump_file.name
    assert r.get("s3_key") is None


def _cfg_fake(monkeypatch) -> svc.S3Config:
    cfg = svc.S3Config(
        endpoint="https://ejemplo.r2.cloudflarestorage.com",
        access_key_id="ak",
        secret_access_key="sk",
        bucket="copias",
    )

    async def get_cfg():
        return cfg

    async def sin_row():
        return None

    monkeypatch.setattr(svc, "get_s3_config", get_cfg)
    monkeypatch.setattr(svc, "get_settings_row", sin_row)
    return cfg


async def test_process_dump_subida_ok_registra_uploaded(monkeypatch, registro, dump_file):
    _cfg_fake(monkeypatch)
    subidas: list[tuple[str, bytes]] = []
    monkeypatch.setattr(svc, "_sync_upload", lambda cfg, key, body: subidas.append((key, body)))
    monkeypatch.setattr(svc, "_sync_list", lambda cfg: [])
    # El objeto del bucket pesa lo que se subió (verificación post-subida).
    monkeypatch.setattr(svc, "_sync_head_size", lambda cfg, key: len(subidas[-1][1]))

    await svc.process_dump(dump_file, kind="scheduled", dump_duration_ms=5)

    assert len(registro) == 1
    r = registro[0]
    assert r["status"] == "ok"
    assert r["upload_status"] == "uploaded"
    assert r["local_file"] == dump_file.name
    assert r["s3_key"].startswith(svc.APP_PREFIX) and r["s3_key"].endswith(svc.KEY_SUFFIX)
    # Lo subido es el blob cifrado (EKB1) y descifra al dump original.
    assert subidas and subidas[0][0] == r["s3_key"]
    blob = subidas[0][1]
    assert blob.startswith(svc.MAGIC)
    assert svc.decrypt_backup_bytes(blob) == dump_file.read_bytes()


async def test_process_dump_subida_fallida_registra_failed_y_relanza(
    monkeypatch, registro, dump_file
):
    _cfg_fake(monkeypatch)

    def upload_roto(cfg, key, body):
        raise RuntimeError("bucket no responde")

    monkeypatch.setattr(svc, "_sync_upload", upload_roto)

    with pytest.raises(RuntimeError):
        await svc.process_dump(dump_file, kind="manual", dump_duration_ms=5)

    assert len(registro) == 1
    r = registro[0]
    assert r["status"] == "failed"
    assert r["upload_status"] == "failed"
    assert r["local_file"] == dump_file.name  # el dump local se conserva
    assert "Subida a S3 fallida" in r["error"]


# ---------------------------------------------------------------------------
# Destino de las copias (selector cloud/server/both)
# ---------------------------------------------------------------------------


def test_resolve_destination_respeta_lo_elegido():
    assert svc.resolve_destination("cloud", True) == "cloud"
    assert svc.resolve_destination("server", True) == "server"
    assert svc.resolve_destination("both", False) == "both"


def test_resolve_destination_sin_eleccion_comportamiento_historico():
    # NULL (instalación anterior al selector): both si hay bucket, server si no.
    assert svc.resolve_destination(None, True) == "both"
    assert svc.resolve_destination(None, False) == "server"
    # Valores corruptos caen al mismo fail-safe.
    assert svc.resolve_destination("marte", True) == "both"


async def test_process_dump_destino_server_no_toca_el_bucket(
    monkeypatch, registro, dump_file
):
    _cfg_fake(monkeypatch)

    def no_debe_subir(cfg, key, body):
        raise AssertionError("con destino server NO debe haber subida")

    monkeypatch.setattr(svc, "_sync_upload", no_debe_subir)

    await svc.process_dump(
        dump_file, kind="manual", dump_duration_ms=5, destination="server"
    )

    assert len(registro) == 1
    r = registro[0]
    assert r["status"] == "ok"
    assert r["upload_status"] == "skipped"
    assert r["local_file"] == dump_file.name
    assert r.get("s3_key") is None
    assert dump_file.exists()  # la copia local se conserva, claro


async def test_process_dump_destino_cloud_borra_el_dump_tras_subir(
    monkeypatch, registro, dump_file
):
    _cfg_fake(monkeypatch)
    subidas: list[tuple[str, bytes]] = []
    monkeypatch.setattr(svc, "_sync_upload", lambda cfg, key, body: subidas.append((key, body)))
    monkeypatch.setattr(svc, "_sync_list", lambda cfg: [])
    monkeypatch.setattr(svc, "_sync_head_size", lambda cfg, key: len(subidas[-1][1]))
    original = dump_file.read_bytes()

    await svc.process_dump(
        dump_file, kind="scheduled", dump_duration_ms=5, destination="cloud"
    )

    assert len(registro) == 1
    r = registro[0]
    assert r["status"] == "ok"
    assert r["upload_status"] == "uploaded"
    assert r["local_file"] is None  # ya no queda fichero en el servidor
    assert not dump_file.exists()  # borrado SOLO tras subida OK
    # Y lo subido descifra al dump original (la copia está a salvo).
    assert svc.decrypt_backup_bytes(subidas[0][1]) == original


async def test_process_dump_destino_cloud_conserva_el_dump_si_la_subida_falla(
    monkeypatch, registro, dump_file
):
    _cfg_fake(monkeypatch)

    def upload_roto(cfg, key, body):
        raise RuntimeError("bucket no responde")

    monkeypatch.setattr(svc, "_sync_upload", upload_roto)

    with pytest.raises(RuntimeError):
        await svc.process_dump(
            dump_file, kind="manual", dump_duration_ms=5, destination="cloud"
        )

    # NUNCA perder la única copia: el dump local sigue ahí y queda registrado.
    assert dump_file.exists()
    r = registro[0]
    assert r["status"] == "failed"
    assert r["upload_status"] == "failed"
    assert r["local_file"] == dump_file.name


async def test_process_dump_destino_cloud_sin_bucket_conserva_el_dump(
    monkeypatch, registro, dump_file
):
    # Eligió cloud pero las credenciales ya no están (borradas a posteriori):
    # jamás borrar el dump local sin haberlo subido antes.
    async def sin_cfg():
        return None

    monkeypatch.setattr(svc, "get_s3_config", sin_cfg)

    await svc.process_dump(
        dump_file, kind="manual", dump_duration_ms=5, destination="cloud"
    )

    assert dump_file.exists()
    r = registro[0]
    assert r["status"] == "ok"
    assert r["upload_status"] == "not_configured"
    assert r["local_file"] == dump_file.name


# ---------------------------------------------------------------------------
# DB-gate (mismo patrón que test_contact_notes.py)
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


@pytestmark_db
async def test_update_settings_acepta_semanal_y_mensual():
    original = await svc.get_settings_row()
    try:
        row = await svc.update_settings(frequency="weekly")
        assert row.frequency == "weekly"
        row = await svc.update_settings(frequency="monthly")
        assert row.frequency == "monthly"
        with pytest.raises(ValueError):
            await svc.update_settings(frequency="quincenal")
    finally:
        await svc.update_settings(frequency=original.frequency if original else "daily")


@pytestmark_db
async def test_update_settings_acepta_destino_y_rechaza_invalidos():
    original = await svc.get_settings_row()
    original_dest = original.destination if original else None
    try:
        row = await svc.update_settings(destination="cloud")
        assert row.destination == "cloud"
        row = await svc.update_settings(destination="server")
        assert row.destination == "server"
        row = await svc.update_settings(destination="both")
        assert row.destination == "both"
        with pytest.raises(ValueError):
            await svc.update_settings(destination="disquete")
    finally:
        from app.db.session import db_session
        from app.models.backup import BackupSettings
        from sqlalchemy import select

        async with db_session() as db:
            row = (await db.execute(select(BackupSettings).limit(1))).scalar_one()
            row.destination = original_dest
            await db.commit()


@pytestmark_db
async def test_claim_scheduled_slot_semanal_sella_y_no_duplica():
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.backup import BackupSettings

    original = await svc.get_settings_row()
    now = madrid(2026, 7, 17, 12, 0).astimezone(timezone.utc)
    try:
        await svc.update_settings(enabled=True, frequency="weekly", daily_hour=4)
        async with db_session() as db:
            row = (await db.execute(select(BackupSettings).limit(1))).scalar_one()
            row.last_scheduled_at = now - timedelta(days=7, hours=8)
            await db.commit()

        assert await svc.claim_scheduled_slot(now=now) is True
        # El sello quedó escrito → un tick 10 min después NO duplica.
        assert await svc.claim_scheduled_slot(now=now + timedelta(minutes=10)) is False
        # Y a los 7 días vuelve a tocar.
        assert await svc.claim_scheduled_slot(now=now + timedelta(days=7)) is True
    finally:
        if original is not None:
            await svc.update_settings(
                enabled=original.enabled,
                frequency=original.frequency,
                daily_hour=original.daily_hour,
            )
            async with db_session() as db:
                row = (await db.execute(select(BackupSettings).limit(1))).scalar_one()
                row.last_scheduled_at = original.last_scheduled_at
                await db.commit()


@pytestmark_db
async def test_record_run_persiste_destino():
    from sqlalchemy import delete

    from app.db.session import db_session
    from app.models.backup import BackupRun

    await svc.record_run(
        kind="manual",
        status="ok",
        s3_key="chatbot/2026-07-16_0400.dump.enc",
        size_bytes=2048,
        local_file="chatbot_20260716_040000.dump",
        upload_status="uploaded",
    )
    runs = await svc.list_runs(limit=1)
    try:
        assert runs[0].local_file == "chatbot_20260716_040000.dump"
        assert runs[0].upload_status == "uploaded"
        assert runs[0].s3_key == "chatbot/2026-07-16_0400.dump.enc"
    finally:
        async with db_session() as db:
            await db.execute(delete(BackupRun).where(BackupRun.id == runs[0].id))
            await db.commit()
