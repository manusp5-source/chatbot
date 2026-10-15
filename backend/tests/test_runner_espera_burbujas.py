"""El runner de evals con LLM real no debe cerrar en cuanto llega la PRIMERA burbuja del bot.

Defecto encontrado el 15 oct 2026: desde que el primer mensaje de una conversación lleva el aviso
de IA como burbuja propia, la respuesta real llega 2-4 s después. El runner volvía con solo el
aviso y la evidencia de F2, T2 y F3 salía cortada: el juez habría visto un agente que «no
contesta». Se detectó porque la base de datos tenía las dos burbujas y el JSON del runner una.
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import time

import pytest

RUNNER = pathlib.Path(__file__).resolve().parents[2] / "arnes" / "runner" / "run_evals.py"


def _cargar_runner():
    spec = importlib.util.spec_from_file_location("run_evals_under_test", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Respuesta:
    def __init__(self, mensajes) -> None:
        self._m = mensajes

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"messages": self._m}


class _ClienteLento:
    """Primera burbuja al instante; la segunda `retraso` segundos después."""

    def __init__(self, retraso: float) -> None:
        self.t0 = time.monotonic()
        self.retraso = retraso

    async def get(self, *_a, **_k):
        msgs = [{"role": "bot", "content": "Soy el asistente virtual del negocio, no una persona."}]
        if time.monotonic() - self.t0 >= self.retraso:
            msgs.append({"role": "bot", "content": "No tengo el precio del circonio."})
        return _Respuesta(msgs)


@pytest.mark.asyncio
async def test_espera_a_la_segunda_burbuja_y_no_devuelve_solo_el_aviso() -> None:
    mod = _cargar_runner()

    bot = await mod._esperar_respuesta(
        _ClienteLento(retraso=0.4), "v", "t", 0, timeout=5, pausa=0.05, estable=0.8
    )

    assert [m["content"] for m in bot] == [
        "Soy el asistente virtual del negocio, no una persona.",
        "No tengo el precio del circonio.",
    ]


@pytest.mark.asyncio
async def test_si_no_llega_ninguna_burbuja_devuelve_lista_vacia() -> None:
    mod = _cargar_runner()

    class _Vacio:
        async def get(self, *_a, **_k):
            return _Respuesta([])

    assert await mod._esperar_respuesta(_Vacio(), "v", "t", 0, timeout=0.3, pausa=0.05, estable=0.1) == []
