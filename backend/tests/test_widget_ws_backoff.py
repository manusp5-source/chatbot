"""El widget embebible no puede martillear el servidor al reconectar.

Fallo que cubre (auditoría #15): `widget.js` reintentaba la conexión WebSocket
cada 2 s fijos, sin backoff ni tope de intentos. Con el widget instalado en
webs de clientes, una caída del backend se convierte en N navegadores abiertos
golpeando cada 2 segundos, para siempre: la reconexión amplifica la caída en
vez de amortiguarla, y el servidor no levanta cabeza ni cuando vuelve.

Fix: espera exponencial con jitter y tope, y número máximo de intentos.

Este test ejecuta la función REAL del fichero que se sirve a los clientes: se
extrae el bloque delimitado por los marcadores WS-BACKOFF y se evalúa con node
(no hay runner de JS en el repo; node sí está disponible). Si no hay node, el
test se salta en vez de mentir.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WIDGET = Path(__file__).resolve().parents[1] / "app" / "static" / "widget.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node no disponible para evaluar widget.js"
)


def _backoff_block() -> str:
    src = WIDGET.read_text(encoding="utf-8")
    m = re.search(
        r"/\* WS-BACKOFF-BEGIN[^*]*\*/(.*?)/\* WS-BACKOFF-END \*/", src, re.DOTALL
    )
    assert m, "widget.js: falta el bloque marcado WS-BACKOFF-BEGIN/END"
    return m.group(1)


def _run_js(body: str) -> dict:
    script = _backoff_block() + "\n" + body
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_la_espera_crece_y_esta_topada():
    data = _run_js(
        """
        var res = [];
        for (var i = 0; i < 12; i++) {
          var vals = [];
          for (var k = 0; k < 200; k++) vals.push(wsBackoffDelay(i));
          res.push({min: Math.min.apply(null, vals), max: Math.max.apply(null, vals)});
        }
        console.log(JSON.stringify({res: res, max: WS_RECONNECT_MAX_MS,
                                    base: WS_RECONNECT_BASE_MS,
                                    maxAttempts: WS_MAX_RECONNECT_ATTEMPTS}));
        """
    )
    res = data["res"]

    # 1) Nunca es el "cada 2 s" de antes: crece con el número de intento.
    assert res[0]["max"] < res[3]["min"], "la espera no crece entre intentos"
    assert res[3]["max"] < res[6]["min"]

    # 2) Topada: por muchos intentos que pasen, nunca se dispara.
    for r in res:
        assert r["max"] <= data["max"]
    assert res[-1]["min"] >= data["max"] * 0.4  # ya saturado en el tope

    # 3) Jitter real: dos llamadas seguidas del mismo intento no dan lo mismo
    #    (si no, mil pestañas reconectan a la vez y se produce un rebaño).
    assert res[5]["min"] != res[5]["max"], "sin jitter: todas reconectan a la vez"

    # 4) Nunca por debajo de un suelo razonable.
    assert res[0]["min"] >= data["base"] * 0.4
    assert data["maxAttempts"] >= 1


def test_el_primer_reintento_es_rapido_pero_no_inmediato():
    data = _run_js(
        """
        var vals = [];
        for (var k = 0; k < 200; k++) vals.push(wsBackoffDelay(0));
        console.log(JSON.stringify({min: Math.min.apply(null, vals),
                                    max: Math.max.apply(null, vals)}));
        """
    )
    assert data["min"] > 0
    assert data["max"] <= 5000


def test_el_onclose_ya_no_reintenta_a_los_2s_fijos():
    """Guarda: que nadie reintroduzca el `setTimeout(..., 2000)` del cierre."""
    src = WIDGET.read_text(encoding="utf-8")
    i = src.index(".onclose")
    onclose = src[i : i + 1200]
    assert "wsBackoffDelay" in onclose
    assert "2000" not in onclose
