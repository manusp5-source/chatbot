"""El lock de ingesta de Gmail solo lo suelta su dueño.

Fallo que cubre (auditoría #13): el `finally` hacía `redis.delete(_LOCK_KEY)` a
secas. El lock tiene un TTL de 110 s, pero una corrida lenta (muchos correos,
Gmail tardón, transcripciones) lo pasa de largo. Secuencia real:

  1. la corrida A coge el lock; tarda más de 110 s y el lock CADUCA;
  2. el beat siguiente, B, coge el lock —legítimamente— y empieza;
  3. A termina y borra el lock… que ya es de B;
  4. a partir de ahí el siguiente beat entra con B todavía dentro.

Dos corridas a la vez sobre el mismo buzón: el `last_history_id` se pisa entre
sí y se pierden correos (o se reprocesan). El mismo `delete` incondicional
también borraba el lock ajeno cuando Redis fallaba al adquirirlo y se seguía
"sin lock".

Fix: valor único por corrida + borrado condicional (compare-and-delete atómico
en Redis). Aquí se prueba `_release_lock`, que es donde vive la decisión.
"""
from __future__ import annotations

import pytest


class _FakeRedis:
    """Redis mínimo: SET NX/EX, GET, DELETE y EVAL del script de borrado."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.eval_calls = 0

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        return int(self.store.pop(key, None) is not None)

    async def eval(self, _script, _numkeys, key, arg):
        # Emula el compare-and-delete del script Lua.
        self.eval_calls += 1
        if self.store.get(key) == arg:
            del self.store[key]
            return 1
        return 0


@pytest.mark.asyncio
async def test_no_borra_el_lock_de_otra_corrida():
    from app.services.gmail_ingest import _LOCK_KEY, _release_lock

    r = _FakeRedis()
    # La corrida A coge el lock...
    token_a = "corrida-a"
    assert await r.set(_LOCK_KEY, token_a, nx=True, ex=110)
    # ...tarda demasiado, el lock caduca y lo coge B.
    r.store[_LOCK_KEY] = "corrida-b"

    # A termina y suelta: NO puede llevarse el lock de B.
    await _release_lock(r, token_a)
    assert r.store.get(_LOCK_KEY) == "corrida-b"


@pytest.mark.asyncio
async def test_el_dueno_si_suelta_su_lock():
    from app.services.gmail_ingest import _LOCK_KEY, _release_lock

    r = _FakeRedis()
    token = "corrida-a"
    assert await r.set(_LOCK_KEY, token, nx=True, ex=110)
    await _release_lock(r, token)
    assert _LOCK_KEY not in r.store


@pytest.mark.asyncio
async def test_sin_token_no_se_toca_nada():
    """Si Redis falló al adquirir y se siguió sin lock, no hay nada que soltar."""
    from app.services.gmail_ingest import _LOCK_KEY, _release_lock

    r = _FakeRedis()
    r.store[_LOCK_KEY] = "de-otro"
    await _release_lock(r, None)
    assert r.store[_LOCK_KEY] == "de-otro"
    assert r.eval_calls == 0


@pytest.mark.asyncio
async def test_un_fallo_de_redis_al_soltar_no_rompe_la_ingesta():
    from app.services.gmail_ingest import _release_lock

    class _Roto(_FakeRedis):
        async def eval(self, *_a, **_kw):
            raise RuntimeError("redis caído")

    await _release_lock(_Roto(), "tok")  # no debe lanzar


@pytest.mark.asyncio
async def test_el_token_es_distinto_en_cada_corrida():
    from app.services.gmail_ingest import _new_lock_token

    assert _new_lock_token() != _new_lock_token()
