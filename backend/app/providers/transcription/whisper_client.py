import io

from openai import AsyncOpenAI

from app.core.config import settings
from app.core.logging import get_logger
from app.providers.openai_factory import TRANSCRIPTION_TIMEOUT_SECONDS, get_async_openai
from app.services.credentials import get_credential
from app.services.runtime_logs import push_runtime_log

logger = get_logger(__name__)


async def _get_client() -> AsyncOpenAI | None:
    api_key = await get_credential("openai_api_key")
    if not api_key:
        return None
    # Más margen que el resto: subir el audio y transcribirlo tarda de verdad.
    return get_async_openai(api_key=api_key, timeout=TRANSCRIPTION_TIMEOUT_SECONDS)


async def transcribe_audio_bytes(audio: bytes, filename: str = "audio.ogg", language: str = "es") -> str:
    client = await _get_client()
    if client is None:
        # La transcripción es SIEMPRE de OpenAI (Whisper). El fallback de LLM
        # (OpenRouter/deepseek/…) NO transcribe: si solo está configurado el
        # fallback y no la clave de OpenAI, las notas de voz no se transcriben.
        # Lo hacemos visible en "Logs en vivo" para que no sea un fallo mudo.
        logger.warning("whisper.no_key")
        await push_runtime_log(
            level="error",
            event="whisper.no_key",
            message="No hay clave de OpenAI configurada: Whisper no puede "
            "transcribir las notas de voz (el fallback de LLM no sirve para "
            "transcripción). Configura openai_api_key en Admin → Credenciales.",
        )
        return ""
    buf = io.BytesIO(audio)
    buf.name = filename
    try:
        resp = await client.audio.transcriptions.create(
            model=settings.OPENAI_WHISPER_MODEL,
            file=buf,
            language=language,
        )
    except Exception as e:  # noqa: BLE001 — se relanza tras dejar traza
        # Sin esto el error de OpenAI solo se ve en el stdout del worker: lo
        # empujamos a "Logs en vivo" para que el fallo nunca vuelva a ser mudo.
        await push_runtime_log(
            level="error",
            event="whisper.error",
            message="OpenAI rechazó la transcripción de una nota de voz.",
            filename=filename,
            model=settings.OPENAI_WHISPER_MODEL,
            error=str(e)[:300],
        )
        raise
    await _track_transcription_usage(resp, len(audio))
    return resp.text or ""


# Ritmo típico de un ogg/opus de nota de voz (~24 kbps). Solo se usa para
# ESTIMAR los tokens cuando la API no devuelve `usage` (p. ej. whisper-1).
_APPROX_BYTES_PER_SECOND = 3000
# gpt-4o-transcribe factura el audio de entrada a ~10 tokens por segundo.
_APPROX_AUDIO_TOKENS_PER_SECOND = 10


async def _track_transcription_usage(resp, audio_bytes: int) -> None:
    """Anota el consumo de la transcripción para que cuente contra el tope.

    Antes no se registraba NADA: en una instalación con notas de voz, el tope de
    gasto medía bastante menos de lo que se gastaba de verdad. Los modelos
    `gpt-4o(-mini)-transcribe` se facturan por tokens y devuelven `usage`; si no
    viene (whisper-1 y compañía), se estima por el tamaño del audio para no
    contar cero. Best-effort: nunca rompe la transcripción.
    """
    try:
        usage = getattr(resp, "usage", None)
        prompt_tokens = 0
        completion_tokens = 0
        if usage is not None:
            prompt_tokens = int(
                getattr(usage, "input_tokens", None) or getattr(usage, "prompt_tokens", 0) or 0
            )
            completion_tokens = int(
                getattr(usage, "output_tokens", None)
                or getattr(usage, "completion_tokens", 0)
                or 0
            )
        estimated = False
        if not prompt_tokens and not completion_tokens:
            seconds = max(1.0, audio_bytes / _APPROX_BYTES_PER_SECOND)
            prompt_tokens = int(seconds * _APPROX_AUDIO_TOKENS_PER_SECOND)
            estimated = True

        from app.services.usage_tracker import track_usage

        await track_usage(
            source="transcription",
            model=settings.OPENAI_WHISPER_MODEL,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        logger.info(
            "whisper.usage",
            model=settings.OPENAI_WHISPER_MODEL,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated=estimated,
        )
    except Exception as e:  # noqa: BLE001 — telemetría, nunca bloquea
        logger.warning("whisper.usage.track_failed", error=str(e))
