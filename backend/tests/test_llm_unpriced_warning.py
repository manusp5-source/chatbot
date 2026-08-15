"""Un modelo sin tarifa tiene que AVISAR, no sumar 0 en silencio.

`estimate_cost_usd` devuelve 0 para un modelo que no está en la tabla de
precios, y eso está bien: no inventamos coste. Lo que no está bien es que sea
silencioso. El tope mensual se calcula sumando el coste estimado de cada
llamada, así que un modelo sin tarifa suma 0 al gasto del mes, el tope nunca
salta y el agente sigue respondiendo (y facturando) como si fuera gratis.

Con Claude y Gemini el riesgo sube: sus ids no siempre casan a la primera con
los de OpenRouter, así que un modelo nuevo puede quedarse sin precio sin que
nadie se entere hasta que llega la factura.
"""
import pytest

from app.providers.llm import telemetry


@pytest.fixture(autouse=True)
def _reset_avisos():
    telemetry._warned_unpriced.clear()
    yield
    telemetry._warned_unpriced.clear()


def _capturar_avisos(monkeypatch):
    avisos: list[dict] = []

    async def _fake_push(**kw):
        avisos.append(kw)

    monkeypatch.setattr(
        "app.services.runtime_logs.push_runtime_log", _fake_push, raising=False
    )
    return avisos


@pytest.mark.asyncio
async def test_avisa_cuando_el_modelo_consume_pero_no_tiene_tarifa(monkeypatch):
    avisos = _capturar_avisos(monkeypatch)
    await telemetry._warn_if_unpriced(
        model="claude-opus-9-9", cost_usd=0.0, tokens=2500, source="agent"
    )
    assert len(avisos) == 1
    assert avisos[0]["level"] == "warn"
    assert avisos[0]["event"] == "llm.model_unpriced"
    # El mensaje tiene que decir el modelo y la consecuencia real.
    assert "claude-opus-9-9" in avisos[0]["message"]
    assert "presupuesto" in avisos[0]["message"]


@pytest.mark.asyncio
async def test_solo_avisa_una_vez_por_modelo(monkeypatch):
    """Esto corre en la ruta caliente de CADA mensaje: sin deduplicar, un
    modelo sin tarifa llenaría el registro del panel."""
    avisos = _capturar_avisos(monkeypatch)
    for _ in range(5):
        await telemetry._warn_if_unpriced(
            model="gemini-9.9-flash", cost_usd=0.0, tokens=100, source="agent"
        )
    assert len(avisos) == 1


@pytest.mark.asyncio
async def test_no_avisa_si_el_modelo_si_tiene_tarifa(monkeypatch):
    avisos = _capturar_avisos(monkeypatch)
    await telemetry._warn_if_unpriced(
        model="claude-opus-5", cost_usd=0.0123, tokens=2500, source="agent"
    )
    assert avisos == []


@pytest.mark.asyncio
async def test_no_avisa_si_no_se_han_consumido_tokens(monkeypatch):
    """Coste 0 con 0 tokens no es un modelo sin tarifa: es una llamada vacía."""
    avisos = _capturar_avisos(monkeypatch)
    await telemetry._warn_if_unpriced(model="lo-que-sea", cost_usd=0.0, tokens=0, source="agent")
    assert avisos == []


@pytest.mark.asyncio
async def test_un_fallo_al_avisar_no_tumba_la_respuesta(monkeypatch):
    """El aviso es best-effort: nunca puede tapar la respuesta al cliente."""

    async def _boom(**_kw):
        raise RuntimeError("redis caído")

    monkeypatch.setattr(
        "app.services.runtime_logs.push_runtime_log", _boom, raising=False
    )
    await telemetry._warn_if_unpriced(
        model="modelo-x", cost_usd=0.0, tokens=100, source="agent"
    )  # no debe lanzar


def test_el_preview_entiende_el_contenido_en_bloques_de_anthropic():
    """En Anthropic el contenido de un turno es una LISTA de bloques, no una
    cadena. Sin esto el panel enseñaría el repr de una lista de dicts."""
    preview = telemetry.build_prompt_preview(
        [
            {"role": "system", "content": "Eres el bot."},
            {"role": "user", "content": [{"type": "text", "text": "¿cuánto cuesta?"}]},
        ]
    )
    assert "[system] Eres el bot." in preview
    assert "[user] ¿cuánto cuesta?" in preview
    assert "type" not in preview  # no se ha colado el dict crudo


def test_el_preview_sigue_funcionando_con_texto_plano_de_openai():
    preview = telemetry.build_prompt_preview(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "hola"}]
    )
    assert preview == "[system] s\n[user] hola"
