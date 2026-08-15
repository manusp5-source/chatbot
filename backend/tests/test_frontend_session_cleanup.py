"""Al cerrar sesión no puede quedar el token en NINGUNA clave de localStorage.

Fallo que cubre (auditoría #14): el token del panel vivía duplicado en dos
sitios —`localStorage["token"]` (lo lee el interceptor de axios, el WebSocket
del inbox y las descargas de media) y dentro del estado persistido de zustand,
`localStorage["chatbot-auth"]`— y no había un único punto que limpiara los
dos. El interceptor de 401 borraba solo `token`: el JWT seguía escrito en
`chatbot-auth`, y al recargar la página el store lo rehidrataba y la app
volvía a creerse con sesión. Además, el historial del Agente Interno
(`internal_agent_session_v1`) sobrevivía al logout: en un ordenador compartido,
el siguiente que entra ve las conversaciones del anterior.

Fix: un único módulo `src/lib/session.ts` con las claves y un
`clearStoredSession()` que las borra todas; lo usan el logout, el interceptor
de 401 y el widget del agente interno.

El repo no tiene runner de JS (ni vitest ni jest). Este test ejecuta el módulo
REAL con node —que ya soporta TypeScript de forma nativa— sobre un
`localStorage` de mentira. Si no hay node, se salta.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
SESSION_TS = FRONTEND / "src" / "lib" / "session.ts"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node no disponible"
)

_HARNESS = """
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
globalThis.window = { localStorage: globalThis.localStorage };
const m = await import(%(mod)s);

// Estado típico de una sesión abierta: el token en su clave y TAMBIÉN dentro
// del estado persistido de zustand, más el historial del agente interno.
m.setToken("JWT-SECRETO");
localStorage.setItem(
  "chatbot-auth",
  JSON.stringify({ state: { token: "JWT-SECRETO", user: { email: "a@b.c" } }, version: 0 })
);
localStorage.setItem(
  "internal_agent_session_v1",
  JSON.stringify([{ role: "user", content: "datos de un cliente" }])
);

const antes = Object.fromEntries(store);
m.clearStoredSession();
const despues = Object.fromEntries(store);
console.log(JSON.stringify({ antes, despues, leido: m.getToken() }));
"""


def _run() -> dict:
    assert SESSION_TS.exists(), f"falta {SESSION_TS}"
    script = _HARNESS % {"mod": json.dumps(SESSION_TS.as_uri())}
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_no_queda_el_token_en_ninguna_clave():
    data = _run()
    # Sanidad: antes SÍ estaba (si no, el test no probaría nada).
    assert any("JWT-SECRETO" in v for v in data["antes"].values())
    # Después: ni rastro, mire uno donde mire.
    for clave, valor in data["despues"].items():
        assert "JWT-SECRETO" not in valor, f"el token sigue en localStorage['{clave}']"
    assert data["leido"] is None


def test_el_historial_del_agente_interno_no_sobrevive_al_logout():
    data = _run()
    assert "internal_agent_session_v1" in data["antes"]
    assert "internal_agent_session_v1" not in data["despues"]


def test_las_claves_estan_centralizadas_y_se_usan():
    """Nadie debe volver a escribir el nombre de la clave a mano."""
    auth = (FRONTEND / "src" / "store" / "auth.ts").read_text(encoding="utf-8")
    api = (FRONTEND / "src" / "services" / "api.ts").read_text(encoding="utf-8")
    for src, nombre in ((auth, "store/auth.ts"), (api, "services/api.ts")):
        assert "@/lib/session" in src, f"{nombre} no usa el módulo de sesión"
        assert 'localStorage.removeItem("token")' not in src, (
            f"{nombre} sigue borrando el token a mano (limpieza parcial)"
        )


def test_el_token_no_se_persiste_en_el_store_de_zustand():
    """Si `partialize` vuelve a incluir el token, se duplica otra vez."""
    auth = (FRONTEND / "src" / "store" / "auth.ts").read_text(encoding="utf-8")
    assert "partialize" in auth, "el store persiste el token junto al resto del estado"
