"""Copias en VERDE sin haber subido NADA al bucket (fallo silencioso).

El fallo que cubre este test: si una credencial `backup_s3_*` está guardada
pero NO se puede descifrar (ENCRYPTION_KEY rotada, valor corrupto),
`get_credential` devolvía `None` — exactamente lo mismo que devuelve cuando la
credencial no existe. `get_s3_config` leía ese `None` como "no hay bucket
configurado" y `process_dump` registraba la copia como **ok /
not_configured**: backup en verde, cero bytes en el bucket. Te enteras el día
que necesitas restaurar.

No necesita Redis ni Postgres: se sustituyen el cliente Redis, la sesión de BD
y el servicio de cifrado por dobles mínimos, de modo que la cadena real
(credentials → backups → process_dump) se ejerce entera.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from app.services import backups as svc

# ---------------------------------------------------------------------------
# Dobles: Redis en memoria, BD con la credencial guardada, cifrado que no abre
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)


class _CredRow:
    def __init__(self, value_encrypted: bytes | None) -> None:
        self.value_encrypted = value_encrypted


class _FakeResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, row) -> None:
        self._row = row

    async def execute(self, _stmt):
        return _FakeResult(self._row)


class _BrokenEncryption:
    """El descifrado falla siempre (clave rotada / valor corrupto)."""

    def decrypt(self, _blob):
        raise ValueError("Fernet: clave inválida para este token")


def _patch_credentials(monkeypatch, *, row, encryption) -> _FakeRedis:
    """Cablea credentials.py contra los dobles y devuelve el Redis falso."""
    from app.services import credentials as creds

    fake_redis = _FakeRedis()

    @asynccontextmanager
    async def fake_db_session():
        yield _FakeDB(row)

    monkeypatch.setattr(creds, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(creds, "db_session", fake_db_session)
    monkeypatch.setattr(creds, "get_encryption_service", lambda: encryption)
    return fake_redis


def _patch_broken_credentials(monkeypatch) -> _FakeRedis:
    """Las 4 credenciales backup_s3_* EXISTEN pero no se pueden descifrar."""
    return _patch_credentials(
        monkeypatch,
        row=_CredRow(b"blob-cifrado-que-no-abre"),
        encryption=_BrokenEncryption(),
    )


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
    path = tmp_path / "chatbot_20260725_040000.dump"
    path.write_bytes(b"PGDMP" + b"x" * 2048)
    return path


# ---------------------------------------------------------------------------
# credentials.py — "no configurada" NO es lo mismo que "no se puede leer"
# ---------------------------------------------------------------------------


async def test_credencial_ilegible_no_se_confunde_con_no_configurada(monkeypatch):
    from app.services.credentials import CredentialUnavailableError, get_credential

    _patch_broken_credentials(monkeypatch)

    # Los llamantes que NO pueden fallar en silencio piden strict=True.
    with pytest.raises(CredentialUnavailableError):
        await get_credential("backup_s3_bucket", strict=True)


async def test_credencial_inexistente_sigue_devolviendo_none(monkeypatch):
    """La credencial que de verdad no está sigue siendo None (no error)."""
    from app.services.credentials import get_credential

    _patch_credentials(monkeypatch, row=None, encryption=_BrokenEncryption())

    assert await get_credential("backup_s3_bucket", strict=True) is None


async def test_credencial_ilegible_no_envenena_la_cache(monkeypatch):
    """Un fallo de descifrado no puede dejar 60s de "no configurada" en Redis.

    Si se cachea el sentinel, el resto de procesos (worker, beat) ve la
    credencial GUARDADA como si no existiera — y ahí es donde el backup sale
    en verde sin subir nada.
    """
    from app.services.credentials import _CACHE_PREFIX, _NULL_SENTINEL, get_credential

    fake_redis = _patch_broken_credentials(monkeypatch)

    assert await get_credential("backup_s3_bucket") is None  # modo laxo: como antes
    assert fake_redis.store.get(f"{_CACHE_PREFIX}backup_s3_bucket") != _NULL_SENTINEL


# ---------------------------------------------------------------------------
# backups.py — la copia NO puede darse por buena si no se pudo leer el destino
# ---------------------------------------------------------------------------


async def test_get_s3_config_credencial_ilegible_no_es_no_configurado(monkeypatch):
    from app.services.credentials import CredentialUnavailableError

    _patch_broken_credentials(monkeypatch)

    with pytest.raises(CredentialUnavailableError):
        await svc.get_s3_config()


async def test_process_dump_credencial_ilegible_no_marca_la_copia_como_ok(
    monkeypatch, registro, dump_file
):
    """EL FALLO: copia en verde, bucket vacío."""
    from app.services.credentials import CredentialUnavailableError

    _patch_broken_credentials(monkeypatch)

    def no_debe_subir(cfg, key, body):
        raise AssertionError("no hay config: no debería intentarse subida")

    monkeypatch.setattr(svc, "_sync_upload", no_debe_subir)

    with pytest.raises(CredentialUnavailableError):
        await svc.process_dump(dump_file, kind="scheduled", dump_duration_ms=10)

    assert len(registro) == 1
    r = registro[0]
    assert r["status"] == "failed"
    assert r["upload_status"] == "failed"
    assert r["local_file"] == dump_file.name  # el dump local se conserva
    assert "no se pudo leer" in (r["error"] or "").lower()
    assert dump_file.exists()


async def test_process_dump_sin_poder_leer_la_config_falla_ruidosamente(
    monkeypatch, registro, dump_file
):
    """Mismo criterio si lo que falla es leer la config (Redis/BD caídos).

    Antes se tragaba la excepción y se registraba ok/not_configured.
    """

    async def config_ilegible():
        raise RuntimeError("Redis no responde")

    monkeypatch.setattr(svc, "get_s3_config", config_ilegible)

    with pytest.raises(RuntimeError):
        await svc.process_dump(dump_file, kind="manual", dump_duration_ms=10)

    assert registro and registro[0]["status"] == "failed"
    assert registro[0]["upload_status"] == "failed"


async def test_destino_efectivo_no_degrada_a_servidor_con_credencial_ilegible(
    monkeypatch,
):
    """Con credenciales ilegibles NO se puede concluir "no hay bucket".

    Si se concluyera, el destino efectivo caería a "server" y process_dump
    registraría ok/skipped: otra vez verde sin copia remota. El llamante
    (backup_db) tiene su propio fail-safe ("both") ante el error.
    """
    from app.services.credentials import CredentialUnavailableError

    _patch_broken_credentials(monkeypatch)

    async def sin_row():
        return None

    monkeypatch.setattr(svc, "get_settings_row", sin_row)

    with pytest.raises(CredentialUnavailableError):
        await svc.get_effective_destination()


async def test_test_connection_distingue_ilegible_de_no_configurado(monkeypatch):
    """El botón "Probar conexión" tiene que decir la verdad, no "faltan"."""
    _patch_broken_credentials(monkeypatch)

    ok, message = await svc.test_connection()

    assert ok is False
    assert "descifrar" in message.lower()
