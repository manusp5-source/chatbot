"""El borrado RGPD y las grabaciones de llamada (I8).

El audio de una llamada no vive en nuestro disco: lo guarda Retell, y su URL
seguía siendo válida después de borrar el contacto. El propio código lo admitía
por escrito en un comentario y ahí se quedaba.

Lo que se comprueba aquí:
  - si el provider expone el borrado por API, se llama con cada `call_id` y el
    informe lo cuenta;
  - si NO existe todavía, el informe NO miente por omisión: dice cuántas
    quedan pendientes y por qué, y marca el borrado como incompleto.

Sin BD: se usa una sesión falsa que devuelve las llamadas del contacto.
"""
from __future__ import annotations

import uuid

import pytest


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _DbConLlamadas:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, *a, **k):
        return _Result(self._rows)


@pytest.mark.asyncio
async def test_se_llama_a_retell_por_cada_grabacion(monkeypatch):
    from app.providers.voice import retell as retell_mod
    from app.services import data_erasure

    borradas: list[str] = []

    async def fake_delete_call(call_id: str) -> bool:
        borradas.append(call_id)
        return True

    monkeypatch.setattr(retell_mod, "delete_call", fake_delete_call, raising=False)

    db = _DbConLlamadas([("call_aaa", "https://retell/x.wav"), ("call_bbb", None)])
    out = await data_erasure._delete_retell_recordings(db, [uuid.uuid4()])

    assert borradas == ["call_aaa", "call_bbb"]
    assert out["total"] == 2 and out["deleted"] == 2 and out["pending"] == 0


@pytest.mark.asyncio
async def test_si_falta_el_helper_el_informe_lo_dice(monkeypatch):
    """Un informe que se calla lo que no ha podido borrar es peor que no
    tenerlo: el responsable RGPD firma un borrado que no ha ocurrido."""
    from app.providers.voice import retell as retell_mod
    from app.services import data_erasure

    for nombre in ("delete_call", "delete_call_recording", "erase_call"):
        monkeypatch.delattr(retell_mod, nombre, raising=False)

    db = _DbConLlamadas([("call_ccc", "https://retell/y.wav")])
    out = await data_erasure._delete_retell_recordings(db, [uuid.uuid4()])

    assert out["total"] == 1 and out["deleted"] == 0 and out["pending"] == 1
    assert "retell" in out["detail"].lower()


@pytest.mark.asyncio
async def test_una_grabacion_que_falla_no_se_da_por_borrada(monkeypatch):
    from app.providers.voice import retell as retell_mod
    from app.services import data_erasure

    async def fake_delete_call(call_id: str) -> bool:
        if call_id == "call_malo":
            raise RuntimeError("403 de Retell")
        return True

    monkeypatch.setattr(retell_mod, "delete_call", fake_delete_call, raising=False)

    db = _DbConLlamadas([("call_bueno", None), ("call_malo", None)])
    out = await data_erasure._delete_retell_recordings(db, [uuid.uuid4()])
    assert out["deleted"] == 1 and out["pending"] == 1
    assert out["detail"]


@pytest.mark.asyncio
async def test_la_limpieza_de_redis_va_despues_del_commit(monkeypatch):
    """Menor: borrar las claves de Redis dentro de `erase_contact` rompía el
    todo-o-nada — si el commit fallaba, los buffers ya estaban borrados."""
    from app.services import data_erasure

    borradas: list[str] = []

    async def fake_redis_cleanup(phone):
        borradas.append(phone)

    monkeypatch.setattr(data_erasure, "_delete_redis_keys_for_phone", fake_redis_cleanup)

    report = {"deleted": True, "_phone_for_redis_cleanup": "+34600000000"}
    devuelto = await data_erasure.finish_erasure(report)
    assert borradas == ["+34600000000"]
    # Y la clave interna no se cuela en el informe que se guarda en auditoría.
    assert "_phone_for_redis_cleanup" not in devuelto
