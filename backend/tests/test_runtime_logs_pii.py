"""Los "Logs en vivo" no pueden enseñar datos personales en claro.

`core/logging.py` filtraba teléfonos, correos y claves, pero solo en el log
estructurado: el buffer de Redis que alimenta la pantalla "Logs en vivo" del
panel se escribía tal cual. Y en ese buffer entran, entre otros, el teléfono del
contacto (services/conversation.py), el teléfono y el motivo del guardarraíl
(services/agent_guardrails.py), el teléfono real y el intentado
(agents/tools/contact_upsert.py) y los primeros 200 caracteres de la respuesta
cruda del proveedor, con el teléfono dentro (providers/whatsapp/ycloud.py).

El arreglo va por el lado bueno: se filtra AL ESCRIBIR, no confiando en que
cada uno de esos sitios se acuerde de enmascarar lo suyo. Estos tests escriben
por la puerta de siempre (`push_runtime_log`) y miran lo que acaba en Redis.
"""
from __future__ import annotations

import json

import pytest


class _FakePipe:
    def __init__(self, store: dict) -> None:
        self.store = store
        self.pushed: list[str] = []
        self.expired: list[int] = []

    def lpush(self, key, value):
        self.pushed.append(value)
        self.store.setdefault(key, []).insert(0, value)

    def ltrim(self, key, a, b):
        self.store[key] = self.store.get(key, [])[a : b + 1]

    def expire(self, key, ttl):
        self.expired.append(ttl)

    async def execute(self):
        return [True] * (len(self.pushed) + len(self.expired))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict = {}
        self.last_pipe: _FakePipe | None = None

    def pipeline(self, transaction: bool = False):
        self.last_pipe = _FakePipe(self.store)
        return self.last_pipe


@pytest.fixture()
def redis_falso(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr("app.services.runtime_logs.get_redis", lambda: r)
    return r


async def _push(**kw):
    from app.services.runtime_logs import push_runtime_log

    await push_runtime_log(**kw)


@pytest.mark.asyncio
async def test_el_telefono_no_llega_al_buffer(redis_falso):
    await _push(
        level="warn",
        event="guardrail.blocked",
        message="Contacto +34611223344 bloqueado por exceso de mensajes",
        telefono="+34611223344",
        motivo="ráfaga",
    )
    crudo = redis_falso.store["runtime:logs"][0]
    assert "+34611223344" not in crudo, (
        "el teléfono del cliente sale en claro por la pantalla 'Logs en vivo'"
    )
    entry = json.loads(crudo)
    assert "<PHONE>" in entry["message"]
    assert entry["telefono"] == "<PHONE>"
    assert entry["motivo"] == "ráfaga"  # lo que no es PII se conserva


@pytest.mark.asyncio
async def test_email_clave_y_respuesta_cruda_del_proveedor(redis_falso):
    await _push(
        level="error",
        event="ycloud.error",
        message='respuesta: {"to":"+34600111222","error":"invalid"}',
        email="cliente@ejemplo.com",
        api_key="sk-abcdefghijklmnopqrstuvwxyz012345",
    )
    crudo = redis_falso.store["runtime:logs"][0]
    for secreto in ("+34600111222", "cliente@ejemplo.com", "sk-abcdefghijklmnopqrstuvwxyz012345"):
        assert secreto not in crudo, f"{secreto} sale sin enmascarar en el buffer"


@pytest.mark.asyncio
async def test_la_linea_sigue_siendo_json_valido_y_legible(redis_falso):
    """El filtro no puede aplicarse sobre el JSON entero: `ts` son 13 dígitos y
    el patrón de teléfono se lo comería, dejando la línea ilegible."""
    await _push(level="info", event="mensaje.recibido", message="Hola", canal="whatsapp")
    entry = json.loads(redis_falso.store["runtime:logs"][0])
    assert isinstance(entry["ts"], int) and entry["ts"] > 10**12
    assert entry["level"] == "info"
    assert entry["event"] == "mensaje.recibido"
    assert entry["message"] == "Hola"
    assert entry["canal"] == "whatsapp"


@pytest.mark.asyncio
async def test_el_buffer_caduca(redis_falso):
    """Recortar a 1000 líneas NO es retención: sin TTL esas 1000 (con lo que
    lleven dentro) se quedan para siempre."""
    from app.services.runtime_logs import RETENTION_SECONDS

    await _push(level="info", event="algo")
    assert redis_falso.last_pipe.expired == [RETENTION_SECONDS]


@pytest.mark.asyncio
async def test_un_objeto_suelto_tambien_se_filtra(redis_falso):
    """Un valor que no es texto lo serializaría `default=str` DESPUÉS del
    filtro: su contenido se colaría sin enmascarar."""

    class _Cosa:
        def __str__(self):
            return "llamada de +34655443322"

    await _push(level="info", event="voice.call", detalle=_Cosa())
    assert "+34655443322" not in redis_falso.store["runtime:logs"][0]
