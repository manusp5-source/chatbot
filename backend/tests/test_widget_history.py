"""El widget tiene que pintar el hilo y no duplicar lo que ya está en pantalla.

Fallos que cubre (auditoría del canal web):

  B1 — el widget guardaba los mensajes SOLO en el DOM. Al abrir el panel hacía
       `if (!msgsEl.children.length) appendBubble(GREETING, "bot")` y nada más,
       y `closePanel()` cerraba el WebSocket. Como la entrega era pub/sub puro,
       cerrar la burbuja mientras el agente pensaba perdía la respuesta. Y el
       identificador de conversación SÍ sobrevivía en localStorage: el bot
       seguía el hilo contra alguien que miraba una pantalla en blanco.

  I6 — cero feedback mientras el bot piensa. La clase CSS `.cbw-bubble.typing`
       existía y `appendBubble` aceptaba la opción, pero no se usaba nunca. El
       visitante enviaba y se quedaba 15-20 segundos mirando una pantalla
       muerta: en la web de un cliente eso se lee como "está roto".

  I7 — al recibir un 409 (conversación cerrada desde el panel) el widget
       borraba el visitor_id además del token, así que el mismo visitante
       aparecía como un contacto NUEVO y se perdían su nombre y su email.

  Menor — `sentIds` declarado y nunca usado.

El bloque de deduplicación se ejecuta de verdad con node (mismo método que
test_widget_ws_backoff.py); el resto son guardas sobre el fichero que se sirve
a las webs de los clientes.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WIDGET = Path(__file__).resolve().parents[1] / "app" / "static" / "widget.js"


def _src() -> str:
    return WIDGET.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Deduplicación: se ejecuta el código real con node
# ---------------------------------------------------------------------------

_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node no disponible para evaluar widget.js"
)


def _dedupe_block() -> str:
    m = re.search(r"/\* DEDUPE-BEGIN[^*]*\*/(.*?)/\* DEDUPE-END \*/", _src(), re.DOTALL)
    assert m, "widget.js: falta el bloque marcado DEDUPE-BEGIN/END"
    return m.group(1)


def _run_js(body: str) -> dict:
    out = subprocess.run(
        ["node", "-e", _dedupe_block() + "\n" + body],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@_node
def test_no_se_repinta_lo_que_ya_esta_en_pantalla():
    """Lo que el visitante escribió (pintado en local) y lo que llegó por el
    WebSocket vuelve en el historial: no puede salir dos veces."""
    data = _run_js(
        """
        var painted = Object.create(null);
        markPainted(painted, "user", "hola");      // pintado en local al enviar
        markPainted(painted, "bot", "buenas");     // llegó por WebSocket
        var historial = [
          {role: "user", text: "hola"},
          {role: "bot",  text: "buenas"},
          {role: "bot",  text: "algo nuevo"}
        ];
        var pintados = [];
        historial.forEach(function (m) {
          if (!consumePainted(painted, m.role, m.text)) pintados.push(m.text);
        });
        console.log(JSON.stringify({pintados: pintados}));
        """
    )
    assert data["pintados"] == ["algo nuevo"]


@_node
def test_el_mismo_texto_dos_veces_no_se_come_uno_de_los_dos():
    """Si el visitante escribe "hola" dos veces y solo una está en pantalla, el
    historial tiene que pintar la otra. Por eso es un CONTADOR y no un 'ya lo
    he visto'."""
    data = _run_js(
        """
        var painted = Object.create(null);
        markPainted(painted, "user", "hola");
        var pintados = [];
        [{role:"user",text:"hola"},{role:"user",text:"hola"}].forEach(function (m) {
          if (!consumePainted(painted, m.role, m.text)) pintados.push(m.text);
        });
        console.log(JSON.stringify({pintados: pintados}));
        """
    )
    assert data["pintados"] == ["hola"]


@_node
def test_el_rol_forma_parte_de_la_clave():
    """Mismo texto del visitante y del bot son dos burbujas distintas."""
    data = _run_js(
        """
        var painted = Object.create(null);
        markPainted(painted, "user", "gracias");
        console.log(JSON.stringify({
          bot: consumePainted(painted, "bot", "gracias"),
          user: consumePainted(painted, "user", "gracias")
        }));
        """
    )
    assert data["bot"] is False
    assert data["user"] is True


# ---------------------------------------------------------------------------
# Guardas sobre el fichero que se sirve a las webs de los clientes
# ---------------------------------------------------------------------------


def test_el_widget_pide_el_historial_al_servidor():
    src = _src()
    assert "/api/v1/webchat/history" in src
    assert "function loadHistory" in src or "loadHistory(" in src


def test_pinta_el_historial_al_abrir_al_reconectar_y_al_volver_a_la_pestana():
    src = _src()
    # Al abrir.
    abrir = src[src.index("async function openPanel") :][:600]
    assert "loadHistory(false)" in abrir
    # Al reconectar el WebSocket (onopen).
    onopen = src[src.index("sock.onopen") :][:500]
    assert "loadHistory(true)" in onopen
    # Al volver a la pestaña.
    assert "visibilitychange" in src
    visibilidad = src[src.index('"visibilitychange"') :][:700]
    assert "loadHistory(true)" in visibilidad


def test_al_reabrir_no_se_pierde_la_identidad_del_visitante():
    """I7: borrar el visitor_id creaba un contacto nuevo y perdía nombre/email."""
    src = _src()
    reset = src[src.index("function resetSession") :]
    reset = reset[: reset.index("\n  async function")]
    assert 'lsDel("session_token")' in reset
    # El visitor_id solo se toca si alguien pide explícitamente un borrado real.
    assert 'lsDel("visitor_id")' in reset
    assert "keepIdentity === false" in reset
    # Y el envío ya no trata el 409 como sesión caducada (el servidor ya no lo
    # devuelve: reabre conversación sobre el mismo contacto).
    envio = src[src.index("formEl.addEventListener") :]
    assert "409" not in envio


def test_hay_indicador_de_escribiendo_y_se_usa():
    """I6: la clase CSS existía y no se usaba nunca."""
    src = _src()
    assert ".cbw-bubble.typing" in src
    assert "function showTyping" in src
    assert "function hideTyping" in src
    # Se enseña al enviar y se quita cuando contesta el bot.
    envio = src[src.index("formEl.addEventListener") :]
    assert "showTyping()" in envio
    onmessage = src[src.index("sock.onmessage") :][:900]
    assert "hideTyping()" in onmessage
    assert '"typing"' in onmessage  # y también por evento del servidor


def test_no_queda_codigo_muerto_ni_eco_duplicado():
    src = _src()
    # `sentIds` estaba declarado y nunca usado.
    assert "sentIds" not in src
    # El eco `message.in` al canal del visitante se quitó del backend; si el
    # widget lo pintara, cada mensaje saldría duplicado (ya pinta el suyo).
    assert "message.in" not in src


def test_el_snippet_sigue_documentando_todo_lo_que_soporta():
    """El panel genera un snippet con solo clave y URL base: al menos que el
    fichero documente las opciones que existen."""
    cabecera = _src()[:2000]
    for atributo in (
        "data-api-key",
        "data-api-base",
        "data-title",
        "data-greeting",
        "data-brand",
        "data-subtitle",
        "data-privacy-url",
    ):
        assert atributo in cabecera, atributo


def test_el_widget_no_trae_dependencias():
    src = _src()
    assert "import " not in src.replace("importante", "")
    assert "require(" not in src
    # Sigue siendo un fichero pequeño: lo carga la web de un cliente.
    assert len(src.encode("utf-8")) < 40_000


def test_las_burbujas_siguen_usando_textContent():
    """Protección XSS que ya estaba bien: el texto del agente NUNCA por HTML."""
    src = _src()
    append = src[src.index("function appendBubble") :][:400]
    assert "textContent" in append
    assert "innerHTML" not in append
