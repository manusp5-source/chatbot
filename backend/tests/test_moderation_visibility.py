"""La moderación es fail-open, pero ya no es muda.

Sin clave devolvía "no marcado" sin ni un log: la moderación llevaba apagada
desde el minuto uno de la instalación y no había NADA — ni en Monitorización ni
en el panel — que lo dijera. El cliente de transcripción sí lo tenía bien: empuja
el fallo a los logs en vivo. Ahora la moderación hace lo mismo.
"""
import pytest

from app.services import moderation as mod


@pytest.fixture(autouse=True)
def _capture(monkeypatch):
    """Sustituye el push a logs en vivo y desactiva el freno de Redis."""
    logs: list[dict] = []

    async def fake_push(**kwargs):
        logs.append(kwargs)

    async def fake_alert_once(reason, message, **extra):
        await fake_push(event=f"moderation.{reason}", message=message, **extra)

    monkeypatch.setattr(mod, "_alert_once", fake_alert_once)
    return logs


@pytest.mark.asyncio
async def test_sin_clave_avisa_y_marca_no_disponible(monkeypatch, _capture):
    async def sin_clave(_key):
        return ""

    monkeypatch.setattr(mod, "get_credential", sin_clave)

    res = await mod.moderate("hola")
    assert res.flagged is False           # fail-open: se sigue adelante
    assert res.available is False         # pero NO es "comprobado y limpio"
    assert _capture, "el fallo tiene que quedar registrado, no ser mudo"
    assert "moderación" in _capture[0]["message"].lower()


@pytest.mark.asyncio
async def test_error_de_la_api_tambien_avisa(monkeypatch, _capture):
    async def con_clave(_key):
        return "k"

    class _Boom:
        async def create(self, **_):
            raise RuntimeError("timeout")

    class _Client:
        moderations = _Boom()

    monkeypatch.setattr(mod, "get_credential", con_clave)
    monkeypatch.setattr(mod, "get_async_openai", lambda **_: _Client())

    res = await mod.moderate("hola")
    assert res.flagged is False
    assert res.available is False
    assert _capture


@pytest.mark.asyncio
async def test_estado_para_el_panel(monkeypatch):
    async def sin_clave(_key):
        return ""

    monkeypatch.setattr(mod, "get_credential", sin_clave)
    estado = await mod.moderation_status()
    assert estado["operativa"] is False
    assert "NO se están moderando" in estado["detalle"]

    async def con_clave(_key):
        return "k"

    monkeypatch.setattr(mod, "get_credential", con_clave)
    estado = await mod.moderation_status()
    assert estado["operativa"] is True


@pytest.mark.asyncio
async def test_texto_vacio_no_gasta_llamada(monkeypatch, _capture):
    async def boom(_key):
        raise AssertionError("no debería consultar credenciales")

    monkeypatch.setattr(mod, "get_credential", boom)
    res = await mod.moderate("   ")
    assert res.flagged is False
    assert res.available is True
