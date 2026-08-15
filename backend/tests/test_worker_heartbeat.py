"""Vigilancia del worker de Celery.

El worker se quedó muerto 20 horas tras un despliegue: ni
transcripciones, ni respuestas del agente, ni tareas nocturnas. El panel siguió
en verde todo el tiempo por dos motivos que estos tests fijan:

  1. `inspect().active()` devuelve None cuando NO hay workers, y el código hacía
     `or {}` → "0 worker(s)" pero con ok=True.
  2. El backlog se leía del Redis de la aplicación en vez del BROKER (otra base
     de datos), así que siempre salía 0 aunque la cola estuviera llena.
"""
import time

import pytest

from app.api import agent_api
from app.tasks.ping import WORKER_HEARTBEAT_KEY, WORKER_HEARTBEAT_TTL_S, _write_heartbeat


class _FakeRedis:
    def __init__(self, value=None):
        self.value = value
        self.sets: list[tuple] = []

    async def get(self, key):
        assert key == WORKER_HEARTBEAT_KEY
        return self.value

    async def set(self, key, value, ex=None):
        self.sets.append((key, value, ex))


@pytest.mark.asyncio
async def test_ping_writes_a_heartbeat(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr("app.core.redis.get_redis", lambda: fake)
    await _write_heartbeat()
    key, value, ex = fake.sets[0]
    assert key == WORKER_HEARTBEAT_KEY
    assert ex == WORKER_HEARTBEAT_TTL_S
    assert abs(int(value) - int(time.time())) < 5


@pytest.mark.asyncio
async def test_seconds_since_heartbeat_without_mark_is_none():
    """Sin marca no podemos decir que el worker esté vivo."""
    assert await agent_api._worker_seconds_since_heartbeat(_FakeRedis(None)) is None


@pytest.mark.asyncio
async def test_seconds_since_heartbeat_counts_the_age():
    fake = _FakeRedis(str(int(time.time()) - 120))
    seconds = await agent_api._worker_seconds_since_heartbeat(fake)
    assert 115 <= seconds <= 130


@pytest.mark.asyncio
async def test_seconds_since_heartbeat_survives_garbage():
    assert await agent_api._worker_seconds_since_heartbeat(_FakeRedis("no-soy-un-numero")) is None


@pytest.mark.asyncio
async def test_backlog_is_read_from_the_broker_not_the_app_redis(monkeypatch):
    """Regresión: el backlog se leía de REDIS_URL (db 0) y el broker es otra db."""
    used: dict = {}

    class _FakeBrokerClient:
        async def llen(self, name):
            used["queue"] = name
            return 7

        async def aclose(self):
            used["closed"] = True

    def _fake_from_url(url, **kwargs):
        used["url"] = url
        return _FakeBrokerClient()

    monkeypatch.setattr("redis.asyncio.from_url", _fake_from_url)
    monkeypatch.setattr(agent_api.settings, "CELERY_BROKER_URL", "redis://broker:6379/1")
    monkeypatch.setattr(agent_api.settings, "REDIS_URL", "redis://app:6379/0")

    assert await agent_api._celery_backlog() == 7
    assert used["url"] == "redis://broker:6379/1"
    assert used["queue"] == "celery"
    assert used["closed"] is True


@pytest.mark.asyncio
async def test_backlog_returns_none_if_the_broker_is_unreachable(monkeypatch):
    def _boom(url, **kwargs):
        raise OSError("sin ruta al broker")

    monkeypatch.setattr("redis.asyncio.from_url", _boom)
    assert await agent_api._celery_backlog() is None
