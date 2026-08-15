"""El tope de gasto del LLM no puede fallar ABIERTO.

El fallo que cubre este test: el guardarraíl del presupuesto solo funcionaba
cuando todo iba bien, que es justo cuando no hace falta.

  1. `track_usage` se tragaba el fallo con un `warning`: si la fila de consumo
     no se escribía, ese gasto quedaba INVISIBLE para siempre. El agente podía
     quemar el presupuesto entero y `current_month_cost_usd()` seguiría
     diciendo "0 $".
  2. `check_budget_and_maybe_pause` envolvía todo en un try/except que salía en
     silencio: si la consulta del coste reventaba, "no sé cuánto llevo gastado"
     se comportaba exactamente igual que "llevo gastado 0" → nunca se pausaba.

El arreglo: el fallo al registrar consumo es RUIDOSO (log de error) y el
consumo perdido se apunta en un contador de Redis que el cálculo del coste
suma; y quedarse ciego ya no equivale a gasto 0 — si lo último que sabíamos
era que estábamos en zona de riesgo (>= 80 % del tope), se pausa.
"""
from __future__ import annotations

from collections import namedtuple
from datetime import datetime, timezone

import pytest

_Row = namedtuple("_Row", "model pt ct")


def _ym() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


# ---------------------------------------------------------------------------
# Dobles
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self, *, broken: bool = False) -> None:
        self.store: dict = {}
        self.hashes: dict[str, dict[str, int]] = {}
        self.broken = broken

    def _check(self):
        if self.broken:
            raise RuntimeError("Redis caído")

    async def get(self, key):
        self._check()
        return self.store.get(key)

    async def set(self, key, value, ex=None, nx=False):
        self._check()
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def delete(self, key):
        self._check()
        self.store.pop(key, None)

    async def hincrby(self, key, field, amount):
        self._check()
        h = self.hashes.setdefault(key, {})
        h[field] = h.get(field, 0) + int(amount)
        return h[field]

    async def hgetall(self, key):
        self._check()
        return dict(self.hashes.get(key, {}))

    async def expire(self, key, ttl):
        self._check()
        return True


class _SpyLogger:
    def __init__(self) -> None:
        self.errors: list[tuple[str, dict]] = []
        self.warnings: list[tuple[str, dict]] = []

    def error(self, event, **kw):
        self.errors.append((event, kw))

    def warning(self, event, **kw):
        self.warnings.append((event, kw))

    def info(self, event, **kw):
        pass


class _BoomSession:
    async def __aenter__(self):
        raise RuntimeError("BD caída al escribir el consumo")

    async def __aexit__(self, *a):
        return False


def _boom_db_session():
    return _BoomSession()


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _VoiceRow:
    """Agregado de telefonía del mes (conversations.call_cost_usd)."""

    def __init__(self, cost=0.0, secs=0, sin_coste=0):
        self.cost = cost
        self.secs = secs
        self.sin_coste = sin_coste


class _FakeDb:
    """Doble de BD para el cálculo del coste del mes.

    Distingue las dos consultas que hace `current_month_cost_usd`: la de tokens
    (`llm_usage_log`) y la de MINUTOS del canal de voz. En estos tests no hay
    llamadas, así que la telefonía suma 0 y el coste sigue saliendo de los
    tokens — que es justo lo que estos casos quieren comprobar.
    """

    def __init__(self, rows, voice=None):
        self.rows = rows
        self.voice = voice if voice is not None else _VoiceRow()

    async def execute(self, statement, *a, **k):
        if "retell_voice" in str(statement):
            return _FakeResult([self.voice])
        return _FakeResult(self.rows)


def _db_session_with(rows):
    class _Ctx:
        async def __aenter__(self):
            return _FakeDb(rows)

        async def __aexit__(self, *a):
            return False

    return lambda: _Ctx()


_PRICES = {"gpt-5.4": {"input": 1000.0, "output": 1000.0}}  # 1000 $ / 1M tokens


# ---------------------------------------------------------------------------
# 1) El fallo al registrar consumo es ruidoso y NO se pierde
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_track_usage_fallido_loguea_error_y_apunta_lo_no_registrado(monkeypatch):
    from app.services import usage_tracker

    redis = _FakeRedis()
    spy = _SpyLogger()
    monkeypatch.setattr(usage_tracker, "db_session", _boom_db_session)
    monkeypatch.setattr(usage_tracker, "get_redis", lambda: redis)
    monkeypatch.setattr(usage_tracker, "logger", spy)

    await usage_tracker.track_usage(
        source="agent", model="gpt-5.4", prompt_tokens=1000, completion_tokens=500
    )

    assert spy.errors, "el fallo al registrar consumo se sigue tragando como warning"
    # Y el consumo perdido queda apuntado para que el tope pueda contarlo.
    pendiente = await usage_tracker.untracked_usage_tokens()
    assert pendiente.get("gpt-5.4") == (1000, 500), (
        "el consumo que no llegó a BD queda invisible: el tope contará 0"
    )


@pytest.mark.asyncio
async def test_track_usage_ok_no_apunta_nada(monkeypatch):
    """El camino bueno no toca el contador de fallback."""
    from app.services import usage_tracker

    redis = _FakeRedis()

    class _OkDb:
        def add(self, obj):
            pass

        async def commit(self):
            pass

    class _Ctx:
        async def __aenter__(self):
            return _OkDb()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(usage_tracker, "db_session", lambda: _Ctx())
    monkeypatch.setattr(usage_tracker, "get_redis", lambda: redis)

    await usage_tracker.track_usage(source="agent", model="gpt-5.4", prompt_tokens=10)

    assert await usage_tracker.untracked_usage_tokens() == {}


# ---------------------------------------------------------------------------
# 2) El coste del mes incluye lo que no se pudo registrar
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coste_del_mes_suma_el_consumo_no_registrado(monkeypatch):
    from app.services import budget, usage_tracker

    redis = _FakeRedis()
    monkeypatch.setattr(usage_tracker, "get_redis", lambda: redis)
    monkeypatch.setattr(budget, "db_session", _db_session_with([]))

    async def fake_prices(db):
        return _PRICES

    monkeypatch.setattr(budget, "get_price_map", fake_prices)

    # BD sin filas, pero 1M de tokens perdidos = 1000 $ reales gastados.
    await usage_tracker.record_untracked_usage("gpt-5.4", 1_000_000, 0)

    cost = await budget.current_month_cost_usd()
    assert cost == pytest.approx(1000.0), (
        "el gasto no registrado se sigue contando como 0"
    )


# ---------------------------------------------------------------------------
# 3) El tope se dispara aunque el consumo no llegara a BD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pausa_aunque_el_consumo_solo_este_en_el_fallback(monkeypatch):
    from app.services import budget, usage_tracker

    redis = _FakeRedis()
    monkeypatch.setattr(usage_tracker, "get_redis", lambda: redis)
    monkeypatch.setattr(budget, "get_redis", lambda: redis)
    monkeypatch.setattr(budget, "db_session", _db_session_with([]))

    async def fake_prices(db):
        return _PRICES

    async def fake_budget():
        return 10.0

    paused: list[bool] = []

    async def fake_set_paused(v):
        paused.append(v)

    async def fake_is_paused():
        return False

    async def noop(**kw):
        return None

    monkeypatch.setattr(budget, "get_price_map", fake_prices)
    monkeypatch.setattr(budget, "_get_budget_usd", fake_budget)
    monkeypatch.setattr(budget, "set_agent_paused", fake_set_paused)
    monkeypatch.setattr(budget, "is_agent_paused", fake_is_paused)
    monkeypatch.setattr(budget, "push_runtime_log", noop)
    monkeypatch.setattr(budget, "notify_security", noop)

    # 100.000 tokens perdidos = 100 $ > tope de 10 $.
    await usage_tracker.record_untracked_usage("gpt-5.4", 100_000, 0)
    await budget.check_budget_and_maybe_pause()

    assert paused == [True], "el tope no se dispara con el consumo no registrado"


# ---------------------------------------------------------------------------
# 4) Quedarse CIEGO no es lo mismo que gasto 0
# ---------------------------------------------------------------------------


def _blind_env(monkeypatch, redis):
    """No se puede calcular el coste (la consulta revienta)."""
    from app.services import budget

    async def boom_cost():
        raise RuntimeError("BD caída al calcular el coste")

    async def fake_budget():
        return 10.0

    monkeypatch.setattr(budget, "get_redis", lambda: redis)
    monkeypatch.setattr(budget, "current_month_cost_usd", boom_cost)
    monkeypatch.setattr(budget, "_get_budget_usd", fake_budget)


@pytest.mark.asyncio
async def test_ciego_con_el_gasto_en_zona_de_riesgo_pausa(monkeypatch):
    from app.services import budget

    redis = _FakeRedis()
    spy = _SpyLogger()
    _blind_env(monkeypatch, redis)
    monkeypatch.setattr(budget, "logger", spy)

    paused: list[bool] = []

    async def fake_set_paused(v):
        paused.append(v)

    async def fake_is_paused():
        return False

    async def noop(**kw):
        return None

    monkeypatch.setattr(budget, "set_agent_paused", fake_set_paused)
    monkeypatch.setattr(budget, "is_agent_paused", fake_is_paused)
    monkeypatch.setattr(budget, "push_runtime_log", noop)
    monkeypatch.setattr(budget, "notify_security", noop)

    # Lo último que supimos: 9,50 $ de 10 $ (95 %).
    await budget._remember_last_known(9.5, 10.0)
    await budget.check_budget_and_maybe_pause()

    assert paused == [True], (
        "nos quedamos ciegos rozando el tope y el agente siguió gastando"
    )
    assert spy.errors, "quedarse sin poder medir el gasto no se loguea como error"


@pytest.mark.asyncio
async def test_ciego_con_el_mes_recien_empezado_no_pausa_pero_avisa(monkeypatch):
    """Fallar cerrado sin tumbar el servicio: si el último dato conocido estaba
    lejos del tope, un hipo de BD no puede apagar el bot — pero sí avisa."""
    from app.services import budget

    redis = _FakeRedis()
    spy = _SpyLogger()
    _blind_env(monkeypatch, redis)
    monkeypatch.setattr(budget, "logger", spy)

    paused: list[bool] = []
    avisos: list[dict] = []

    async def fake_set_paused(v):
        paused.append(v)

    async def fake_is_paused():
        return False

    async def noop(**kw):
        return None

    async def fake_notify(**kw):
        avisos.append(kw)

    monkeypatch.setattr(budget, "set_agent_paused", fake_set_paused)
    monkeypatch.setattr(budget, "is_agent_paused", fake_is_paused)
    monkeypatch.setattr(budget, "push_runtime_log", noop)
    monkeypatch.setattr(budget, "notify_security", fake_notify)

    await budget._remember_last_known(0.4, 10.0)  # 4 % del tope
    await budget.check_budget_and_maybe_pause()

    assert paused == [], "un fallo puntual de BD con el mes recién empezado apagó el bot"
    assert avisos, "nos quedamos ciegos y nadie se entera"
    assert spy.errors


@pytest.mark.asyncio
async def test_el_check_bueno_deja_el_ultimo_coste_conocido(monkeypatch):
    """Sin este rastro, la comprobación a ciegas no tendría con qué decidir."""
    from app.services import budget

    redis = _FakeRedis()
    monkeypatch.setattr(budget, "get_redis", lambda: redis)
    monkeypatch.setattr(budget, "db_session", _db_session_with([_Row("gpt-5.4", 1000, 0)]))

    async def fake_prices(db):
        return _PRICES

    async def fake_budget():
        return 10.0

    async def noop(**kw):
        return None

    async def fake_is_paused():
        return False

    monkeypatch.setattr(budget, "get_price_map", fake_prices)
    monkeypatch.setattr(budget, "_get_budget_usd", fake_budget)
    monkeypatch.setattr(budget, "is_agent_paused", fake_is_paused)
    monkeypatch.setattr(budget, "push_runtime_log", noop)
    monkeypatch.setattr(budget, "notify_security", noop)

    await budget.check_budget_and_maybe_pause()

    last = await budget._last_known()
    assert last is not None
    assert last["cost"] == pytest.approx(1.0)
    assert last["budget"] == pytest.approx(10.0)
