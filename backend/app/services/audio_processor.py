"""Descarga + transcripción de audios de WhatsApp."""
import os
import uuid

from sqlalchemy import select

from app.core.config import settings
from app.core.events import event_bus, inbox_channel
from app.core.logging import get_logger
from app.core.redis import get_redis
from app.db.session import db_session
from app.models.message import Message
from app.providers.transcription import transcribe_audio_bytes
from app.providers.whatsapp import get_whatsapp_provider
from app.services.agent_guardrails import MAX_AUDIO_BYTES
from app.services.agent_pause import is_channel_transcription_enabled
from app.services.runtime_logs import push_runtime_log
from app.services.security_alerts import notify_security

logger = get_logger(__name__)

# Texto que dejamos en audio_transcript cuando NO se pudo transcribir (vacío,
# sin clave, o error tras reintentos). Es no vacío a propósito: así el operador
# ve la nota en el inbox y la conversación deja de quedar en agujero negro /
# bucle de re-encolado (la condición `not audio_transcript` deja de cumplirse).
AUDIO_TRANSCRIBE_FAILED = "[Audio recibido — no se pudo transcribir automáticamente]"


def _audio_extension(audio_bytes: bytes, mime: str | None) -> str:
    """Extensión REAL del audio, para que Whisper acepte el archivo.

    OpenAI infiere el formato por el nombre del archivo: un ".bin" lo rechaza
    con 400 aunque los bytes sean válidos. Las notas de voz de Instagram llegan
    como video/mp4 (contenedor MP4/AAC), no como el OGG de WhatsApp, así que
    detectamos por bytes mágicos primero y por mime después.
    """
    if audio_bytes[:4] == b"OggS":
        return ".ogg"
    if audio_bytes[4:8] == b"ftyp":  # contenedor ISO/MP4 (nota de voz IG)
        return ".mp4"
    if audio_bytes[:4] == b"RIFF":
        return ".wav"
    if audio_bytes[:3] == b"ID3" or audio_bytes[:2] in (b"\xff\xfb", b"\xff\xf3"):
        return ".mp3"
    if audio_bytes[:4] == b"\x1a\x45\xdf\xa3":
        return ".webm"
    m = (mime or "").lower()
    for token, ext in (
        ("ogg", ".ogg"), ("opus", ".ogg"),
        ("mp4", ".mp4"), ("m4a", ".m4a"), ("aac", ".m4a"),
        ("mpeg", ".mp3"), ("mp3", ".mp3"),
        ("wav", ".wav"), ("webm", ".webm"), ("flac", ".flac"),
    ):
        if token in m:
            return ext
    # Último recurso: mp3 es la extensión que más formatos "cuela" en Whisper;
    # .bin garantiza el rechazo.
    return ".mp3"


# TTL del cerrojo de encolado. Cubre de sobra descarga + Whisper + reintentos
# (max_retries=3 con countdown=15) sin quedarse pegado si algo se cae por medio.
_ENQUEUED_TTL = 900


async def enqueue_transcription(
    message_id: uuid.UUID | str, canal: str, *, origin: str
) -> bool:
    """Encola la transcripción de una nota de voz. Devuelve True si la encoló.

    Punto ÚNICO de encolado, por dos motivos:

    - El canal puede tener la transcripción apagada (`agent:transcribe_audio:*`).
      Es el único freno: ni la pausa ni el estado de la conversación cortan aquí.
    - Se llama desde dos sitios (al guardar el mensaje entrante y, como red de
      seguridad, al drenar el buffer). El cerrojo `nx` evita pagar dos veces la
      misma transcripción cuando ambos caminos coinciden.
    """
    mid = str(message_id)
    if not await is_channel_transcription_enabled(canal):
        logger.info("audio.transcription.disabled", message_id=mid, canal=canal)
        await push_runtime_log(
            level="info",
            event="audio.transcription.disabled",
            message=f"Nota de voz recibida, pero la transcripción está apagada en {canal}.",
            message_id=mid,
            channel=canal,
        )
        return False

    first = await get_redis().set(
        f"audio:transcribe:enqueued:{mid}", "1", nx=True, ex=_ENQUEUED_TTL
    )
    if not first:
        logger.info("audio.transcription.already_enqueued", message_id=mid, origin=origin)
        return False

    from app.tasks.transcribe_audio import transcribe_audio as task_transcribe
    task_transcribe.delay(mid)
    logger.info("audio.transcription.enqueued", message_id=mid, canal=canal, origin=origin)
    await push_runtime_log(
        level="info",
        event="audio.transcription.enqueued",
        message="Nota de voz recibida: encolada su transcripción.",
        message_id=mid,
        channel=canal,
    )
    return True


async def mark_transcription_failed(message_id: uuid.UUID, reason: str) -> None:
    """Marca un audio como no transcribible tras agotar reintentos.

    Evita el agujero negro: el operador verá la nota en el inbox (como mensaje
    pendiente del cliente) y la atenderá a mano. No re-encolamos el agente: el
    bot no puede entender el audio, así que lo gestiona una persona.
    """
    async with db_session() as db:
        msg = (
            await db.execute(select(Message).where(Message.id == message_id))
        ).scalar_one_or_none()
        if not msg or msg.audio_transcript:
            return
        msg.audio_transcript = AUDIO_TRANSCRIBE_FAILED
        await db.commit()
    await push_runtime_log(
        level="error",
        event="audio.transcribe_failed",
        message="No se pudo transcribir un audio tras varios intentos.",
        message_id=str(message_id),
        reason=(reason or "")[:200],
    )


async def transcribe_message_audio(message_id: uuid.UUID) -> None:
    # Lo PRIMERO: dejar constancia de que la tarea ha arrancado de verdad. Antes
    # este aviso estaba detrás de la consulta a la BD, así que si el worker se
    # colgaba al conectar no aparecía nada y no había forma de distinguir "la
    # tarea no se ejecutó" de "la tarea se ejecutó y falló".
    await push_runtime_log(
        level="info",
        event="audio.transcribe.start",
        message="Iniciando transcripción de una nota de voz.",
        message_id=str(message_id),
    )
    async with db_session() as db:
        msg = (await db.execute(select(Message).where(Message.id == message_id))).scalar_one_or_none()
        if not msg or not msg.audio_url:
            # Diagnóstico: la tarea corrió pero no hay audio que transcribir (el
            # mensaje no se guardó con audio_url). Hacerlo visible evita el
            # "no pasa nada" silencioso.
            await push_runtime_log(
                level="warn",
                event="audio.transcribe.no_url",
                message="Tarea de transcripción sin audio_url (¿no se capturó la nota de voz?).",
                message_id=str(message_id),
            )
            return
        if msg.audio_transcript:
            return  # ya transcrito
        audio_url = msg.audio_url
        # Canal de la conversación: decide POR DÓNDE se descarga el audio. El
        # resto del pipeline (Whisper, guardado, re-encolado) es agnóstico.
        from app.models.conversation import Conversation, ConversationCanal
        canal = (
            await db.execute(
                select(Conversation.canal).where(Conversation.id == msg.conversation_id)
            )
        ).scalar_one_or_none()

    # Instagram descarga de URLs de Meta (lookaside); WhatsApp sigue por YCloud.
    if canal == ConversationCanal.instagram_dm:
        from app.providers.instagram import get_instagram_provider
        audio_bytes, mime = await get_instagram_provider().download_audio(audio_url)
    else:
        wa = get_whatsapp_provider()
        audio_bytes, mime = await wa.download_audio(audio_url)

    # Guardrail: rechaza audios desmesurados (DoS económico a Whisper).
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        logger.warning(
            "audio.too_large",
            message_id=str(message_id),
            bytes=len(audio_bytes),
            max=MAX_AUDIO_BYTES,
        )
        await notify_security(
            kind="audio_too_large",
            title="Audio rechazado por tamaño",
            details={"message_id": str(message_id), "bytes": len(audio_bytes)},
            throttle_key=str(message_id),
        )
        async with db_session() as db:
            msg = (await db.execute(select(Message).where(Message.id == message_id))).scalar_one()
            msg.audio_transcript = "[audio omitido: demasiado grande]"
            await db.commit()
        return

    # Guarda audio local
    ext = _audio_extension(audio_bytes, mime)
    os.makedirs(settings.AUDIO_STORAGE_PATH, exist_ok=True)
    local_path = os.path.join(settings.AUDIO_STORAGE_PATH, f"{message_id}{ext}")
    with open(local_path, "wb") as f:
        f.write(audio_bytes)

    transcript = await transcribe_audio_bytes(audio_bytes, filename=f"audio{ext}", language="es")

    if not transcript.strip():
        # Transcripción vacía (clave OpenAI ausente, audio mudo o Whisper devolvió
        # ""). No dejamos el audio en agujero negro ni en bucle: guardamos el
        # audio local (para poder reproducirlo) + un placeholder y avisamos en el
        # panel. No re-encolamos el agente; la nota la atiende una persona.
        async with db_session() as db:
            msg = (await db.execute(select(Message).where(Message.id == message_id))).scalar_one()
            msg.audio_url = f"/audios/{message_id}{ext}"
            msg.audio_transcript = AUDIO_TRANSCRIBE_FAILED
            await db.commit()
        logger.warning("audio.transcribed_empty", message_id=str(message_id))
        await push_runtime_log(
            level="warn",
            event="audio.transcribe_empty",
            message="Audio recibido pero la transcripción salió vacía (revisar clave OpenAI/Whisper).",
            message_id=str(message_id),
        )
        return

    logger.info("audio.transcribed", message_id=str(message_id), chars=len(transcript))

    async with db_session() as db:
        msg = (await db.execute(select(Message).where(Message.id == message_id))).scalar_one()
        msg.audio_url = f"/audios/{message_id}{ext}"  # ahora referenciado por path local
        msg.audio_transcript = transcript
        await db.commit()

    try:
        await event_bus.publish(
            inbox_channel(),
            "message.updated",
            {"message_id": str(message_id), "audio_transcript": transcript},
        )
    except Exception:
        pass

    # Re-encola procesamiento del mensaje ahora que ya tiene texto
    from app.tasks.process_message import process_message as task_process
    task_process.delay(str(message_id))
