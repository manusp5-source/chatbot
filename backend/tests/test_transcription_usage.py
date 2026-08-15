"""La transcripción de audio ya cuenta contra el tope de gasto.

`whisper_client` no registraba NADA y el modelo que usa ni siquiera estaba en la
tabla de precios: en una instalación con notas de voz, el tope medía bastante
menos de lo que se gastaba de verdad.
"""
import pytest

from app.providers.transcription import whisper_client as wc


class _Resp:
    def __init__(self, usage=None, text="hola"):
        self.usage = usage
        self.text = text


class _Usage:
    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


@pytest.fixture
def registrado(monkeypatch):
    llamadas: list[dict] = []

    async def fake_track(**kwargs):
        llamadas.append(kwargs)

    import app.services.usage_tracker as ut

    monkeypatch.setattr(ut, "track_usage", fake_track)
    return llamadas


@pytest.mark.asyncio
async def test_usa_el_usage_que_devuelve_la_api(registrado):
    await wc._track_transcription_usage(_Resp(usage=_Usage(1200, 40)), 60_000)

    assert len(registrado) == 1
    reg = registrado[0]
    assert reg["source"] == "transcription"
    assert reg["prompt_tokens"] == 1200
    assert reg["completion_tokens"] == 40


@pytest.mark.asyncio
async def test_sin_usage_se_estima_en_vez_de_contar_cero(registrado):
    """whisper-1 no devuelve `usage`: peor estimar que contar 0 $."""
    await wc._track_transcription_usage(_Resp(usage=None), 90_000)

    assert len(registrado) == 1
    assert registrado[0]["prompt_tokens"] > 0


@pytest.mark.asyncio
async def test_el_fallo_del_telemetry_no_rompe_la_transcripcion(monkeypatch):
    import app.services.usage_tracker as ut

    async def boom(**_):
        raise RuntimeError("BD caída")

    monkeypatch.setattr(ut, "track_usage", boom)
    # No debe propagar.
    await wc._track_transcription_usage(_Resp(usage=_Usage(10, 1)), 1000)


def test_el_modelo_de_transcripcion_tiene_precio():
    from app.core.config import settings
    from app.services.llm_pricing import PRICE_PER_1M_USD

    assert settings.OPENAI_WHISPER_MODEL in PRICE_PER_1M_USD
