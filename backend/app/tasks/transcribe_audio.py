import asyncio
import uuid

from celery import shared_task
from celery.exceptions import MaxRetriesExceededError

from app.core.logging import get_logger

logger = get_logger(__name__)


@shared_task(name="app.tasks.transcribe_audio.transcribe_audio", bind=True, max_retries=3)
def transcribe_audio(self, message_id: str) -> None:
    from app.services.audio_processor import (
        mark_transcription_failed,
        transcribe_message_audio,
    )

    try:
        asyncio.run(transcribe_message_audio(uuid.UUID(message_id)))
    except Exception as exc:
        logger.error("transcribe_audio.error", message_id=message_id, error=str(exc))
        try:
            raise self.retry(exc=exc, countdown=15)
        except MaxRetriesExceededError:
            # Agotados los reintentos (descarga/Whisper siguen fallando): NO
            # dejamos el audio en agujero negro — lo marcamos para que el
            # operador lo vea y lo atienda en el inbox.
            logger.error("transcribe_audio.giveup", message_id=message_id)
            asyncio.run(mark_transcription_failed(uuid.UUID(message_id), str(exc)))
