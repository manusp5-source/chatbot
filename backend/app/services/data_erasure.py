"""Borrado y retención de datos personales (RGPD Art. 17 y 5.1.e).

- `erase_contact`: borrado COMPLETO de un contacto — además del cascade de BD
  (conversaciones, mensajes, notas, actividad, correcciones), elimina lo que
  quedaba huérfano: ficheros de audio/media en disco, huecos de conocimiento
  derivados de sus mensajes, destinatarios de envíos masivos con su teléfono y
  claves de Redis. Cierra el "derecho de supresión" que antes dejaba PII suelta.

- `purge_old_audio_files`: retención de las notas de voz en disco (lo más pesado
  y sensible). Borra los FICHEROS de audio más antiguos que la ventana, conserva
  la transcripción (texto) y marca `media_purged_at`.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def _disk_path_for_served_url(url: str | None) -> str | None:
    """Mapea una URL servida (`/audios/x`, `/uploads/x`) a su fichero en disco,
    validando que queda DENTRO del volumen permitido (anti path-traversal).
    Devuelve None si es una URL externa (audio aún no descargado) o insegura."""
    if not url or not url.startswith("/"):
        return None  # URL externa (Meta/YCloud) — no hay fichero local
    if url.startswith("/audios/"):
        name = url[len("/audios/"):]
        root = settings.AUDIO_STORAGE_PATH
    elif url.startswith("/uploads/"):
        name = url[len("/uploads/"):]
        root = settings.UPLOADS_PATH
    else:
        return None
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    abs_path = os.path.join(root, name)
    try:
        real_root = os.path.realpath(root)
        real_target = os.path.realpath(abs_path)
    except OSError:
        return None
    if real_target != real_root and not real_target.startswith(real_root + os.sep):
        return None
    return abs_path


def _unlink_quiet(path: str | None) -> bool:
    if not path:
        return False
    try:
        if os.path.isfile(path):
            os.remove(path)
            return True
    except OSError as e:
        logger.warning("erasure.unlink_failed", path="<path>", error=str(e))
    return False


async def _delete_media_files_for_conversations(
    db: AsyncSession, conversation_ids: list[uuid.UUID]
) -> int:
    """Borra los ficheros de audio/media en disco de todos los mensajes de esas
    conversaciones. Devuelve el nº de ficheros eliminados."""
    if not conversation_ids:
        return 0
    from app.models.message import Message

    rows = (
        await db.execute(
            select(Message.audio_url, Message.media_url).where(
                Message.conversation_id.in_(conversation_ids)
            )
        )
    ).all()
    deleted = 0
    for audio_url, media_url in rows:
        if _unlink_quiet(_disk_path_for_served_url(audio_url)):
            deleted += 1
        if _unlink_quiet(_disk_path_for_served_url(media_url)):
            deleted += 1
    return deleted


async def _delete_redis_keys_for_phone(phone: str | None) -> None:
    """Limpia las claves de Redis asociadas al teléfono (guardrails y buffers
    con el formato HEREDADO). Best-effort: si Redis no responde, no bloquea el
    borrado.

    Las claves del buffer que hoy usa el runtime van por CONVERSACIÓN, no por
    teléfono: las borra `_delete_redis_buffer_keys_for_conversations`. Estas de
    aquí se mantienen porque en una instalación recién actualizada puede quedar
    alguna ráfaga encolada con la clave vieja (ver `services/message_buffer.py`).
    """
    if not phone:
        return
    try:
        from app.core.redis import get_redis
        from app.services.message_buffer import KEY_LAST, KEY_QUEUE

        r = get_redis()
        keys = [
            KEY_LAST.format(phone=phone),
            KEY_QUEUE.format(phone=phone),
            f"guard:msg:{phone}",
            f"guard:llm:{phone}",
            f"guard:hits:{phone}",
            f"guard:blocked:{phone}",
        ]
        await r.delete(*keys)
    except Exception as e:
        logger.warning("erasure.redis_cleanup_failed", error=str(e))


async def _delete_redis_buffer_keys_for_conversations(
    conversation_ids: list[str] | list[uuid.UUID] | None,
) -> int:
    """Borra el buffer de mensajes de cada conversación del contacto.

    El buffer dejó de agruparse por identificador de contacto y pasó a
    agruparse por conversación (`msg_buffer:queue:conv:<id>`, ver
    `services/message_buffer.py`), pero el borrado RGPD seguía intentando las
    claves del formato viejo: o sea, ya no borraba nada. Tras un derecho de
    supresión quedaban en Redis identificadores de mensajes de esa persona, y
    esa cola NO CADUCA (se crea con RPUSH, sin TTL; el único con expiración es
    el marcador `:last:`). Los ids de conversación hay que recogerlos ANTES del
    borrado en cascada: después ya no existen en ninguna parte.

    Devuelve cuántas claves se pidieron borrar (para el informe). Best-effort.
    """
    if not conversation_ids:
        return 0
    try:
        from app.core.redis import get_redis
        from app.services.message_buffer import (
            KEY_LAST,
            KEY_QUEUE,
            conversation_key,
        )

        keys: list[str] = []
        for conv_id in conversation_ids:
            buffer_key = conversation_key(conv_id)
            keys.append(KEY_LAST.format(phone=buffer_key))
            keys.append(KEY_QUEUE.format(phone=buffer_key))
        await get_redis().delete(*keys)
        return len(keys)
    except Exception as e:
        logger.warning("erasure.redis_buffer_cleanup_failed", error=str(e))
        return 0


async def _delete_retell_recordings(db: AsyncSession, conversation_ids: list[uuid.UUID]) -> dict:
    """Borra en Retell las grabaciones de las llamadas de ese contacto.

    El audio de una llamada NO vive en nuestro disco: lo guarda Retell y la URL
    de la grabación sigue siendo válida después de borrar el contacto. Es decir,
    el "derecho de supresión" dejaba fuera justo la grabación de la voz del
    cliente. Solo se puede cerrar llamando a la API de Retell.

    El helper HTTP pertenece al proveedor (`app/providers/voice/retell.py`).
    Aquí se busca por nombre y, si todavía no existe, NO se finge: el informe
    dice cuántas grabaciones quedan pendientes y por qué. Un informe que calla
    lo que no ha podido borrar es peor que no tener informe.

    Devuelve {"total", "deleted", "pending", "detail"}.
    """
    out = {"total": 0, "deleted": 0, "pending": 0, "detail": ""}
    if not conversation_ids:
        return out
    from app.models.conversation import Conversation, ConversationCanal

    rows = (
        await db.execute(
            select(Conversation.session_id, Conversation.call_recording_url).where(
                Conversation.id.in_(conversation_ids),
                Conversation.canal == ConversationCanal.retell_voice,
            )
        )
    ).all()
    calls = [(sid, url) for sid, url in rows if sid]
    out["total"] = len(calls)
    if not calls:
        return out

    # Punto de enganche: el provider expone (o expondrá) el borrado por call_id.
    delete_call = None
    try:
        from app.providers.voice import retell as _retell

        for name in ("delete_call", "delete_call_recording", "erase_call"):
            fn = getattr(_retell, name, None)
            if callable(fn):
                delete_call = fn
                break
    except Exception as e:  # pragma: no cover - import defensivo
        logger.warning("erasure.retell.import_failed", error=str(e))

    if delete_call is None:
        out["pending"] = len(calls)
        out["detail"] = (
            "Las grabaciones siguen en Retell: falta el borrado por API en "
            "app/providers/voice/retell.py (delete_call). Hasta que exista hay que "
            "borrarlas a mano desde el panel de Retell."
        )
        logger.error(
            "erasure.retell.not_implemented",
            calls=len(calls),
            msg="Borrado RGPD incompleto: grabaciones de llamada sin borrar en Retell",
        )
        return out

    for call_id, _url in calls:
        try:
            ok = await delete_call(call_id)
        except Exception as e:  # noqa: BLE001 — una llamada mala no aborta el resto
            ok = False
            logger.warning("erasure.retell.delete_failed", error=str(e))
        if ok:
            out["deleted"] += 1
        else:
            out["pending"] += 1
    if out["pending"]:
        out["detail"] = (
            f"{out['pending']} grabación(es) no se pudieron borrar en Retell. "
            "Revisa la API key del canal de voz y bórralas a mano si insiste."
        )
        logger.error("erasure.retell.incomplete", pending=out["pending"])
    return out


async def erase_contact(db: AsyncSession, contact_id: uuid.UUID) -> dict:
    """Borrado COMPLETO de un contacto y su rastro. Devuelve un pequeño informe.

    Debe llamarse con una sesión que el llamante confirme (commit) después.

    La limpieza de Redis NO se hace aquí: va en `finish_erasure(report)`, que el
    llamante ejecuta DESPUÉS del commit. Antes se borraban las claves antes de
    confirmar la transacción, así que un fallo al confirmar dejaba el contacto
    vivo en la BD pero sus buffers y contadores antispam ya borrados — ni todo
    ni nada.
    """
    from app.models.contact import Contact
    from app.models.conversation import Conversation
    from app.models.knowledge_gap import KnowledgeGap
    from app.models.outbound_job import OutboundJobRecipient

    contact = (
        await db.execute(select(Contact).where(Contact.id == contact_id))
    ).scalar_one_or_none()
    if not contact:
        return {"deleted": False, "reason": "not_found"}

    phone = contact.telefono
    conv_ids = list(
        (
            await db.execute(
                select(Conversation.id).where(Conversation.contact_id == contact_id)
            )
        ).scalars().all()
    )

    # 1) Ficheros de audio/media en disco (el cascade de BD NO los toca).
    files_deleted = await _delete_media_files_for_conversations(db, conv_ids)

    # 1b) Grabaciones de llamada, que viven en Retell y no en nuestro disco.
    recordings = await _delete_retell_recordings(db, conv_ids)

    # 2) Huecos de conocimiento derivados de sus mensajes (FK era SET NULL →
    #    sobrevivían con la pregunta literal del cliente). Se borran.
    gaps_deleted = 0
    if conv_ids:
        gap_rows = (
            await db.execute(
                select(KnowledgeGap).where(KnowledgeGap.conversation_id.in_(conv_ids))
            )
        ).scalars().all()
        for g in gap_rows:
            await db.delete(g)
            gaps_deleted += 1

    # 3) Destinatarios de envíos masivos con su teléfono (sin FK al contacto).
    recips_deleted = 0
    if phone:
        recip_rows = (
            await db.execute(
                select(OutboundJobRecipient).where(OutboundJobRecipient.phone == phone)
            )
        ).scalars().all()
        for rc in recip_rows:
            await db.delete(rc)
            recips_deleted += 1

    # 4) El contacto (cascade: conversaciones → mensajes → traces/correcciones,
    #    notas, actividad, tags).
    await db.delete(contact)

    logger.info(
        "erasure.contact",
        contact_id=str(contact_id),
        conversations=len(conv_ids),
        files_deleted=files_deleted,
        gaps_deleted=gaps_deleted,
        recipients_deleted=recips_deleted,
        call_recordings_deleted=recordings["deleted"],
        call_recordings_pending=recordings["pending"],
    )
    report = {
        "deleted": True,
        "conversations": len(conv_ids),
        "files_deleted": files_deleted,
        "knowledge_gaps_deleted": gaps_deleted,
        "outbound_recipients_deleted": recips_deleted,
        # Grabaciones de llamada (Retell). El informe dice la verdad aunque
        # queden pendientes: es lo que hay que enseñar al responsable RGPD.
        "call_recordings": recordings["total"],
        "call_recordings_deleted": recordings["deleted"],
        "call_recordings_pending": recordings["pending"],
        "complete": recordings["pending"] == 0,
        # 5) Claves de Redis: las borra `finish_erasure` DESPUÉS del commit.
        #    Los ids de conversación se llevan AQUÍ porque en cuanto se
        #    confirme el cascade ya no habrá forma de saber cuáles eran, y el
        #    buffer se agrupa por conversación.
        "_phone_for_redis_cleanup": phone,
        "_conversations_for_redis_cleanup": [str(c) for c in conv_ids],
    }
    if recordings["detail"]:
        report["warning"] = recordings["detail"]
    return report


async def finish_erasure(report: dict) -> dict:
    """Segunda mitad del borrado, para llamar DESPUÉS del commit.

    Limpia lo que no vive en la transacción de BD (claves de Redis). Se separa
    a propósito: si el commit falla, esto no llega a ejecutarse y la BD y Redis
    siguen contando la misma historia.
    """
    if not isinstance(report, dict):
        return report
    phone = report.pop("_phone_for_redis_cleanup", None)
    conv_ids = report.pop("_conversations_for_redis_cleanup", None)
    if phone:
        await _delete_redis_keys_for_phone(phone)
    # Buffers por conversación (los que de verdad usa el runtime hoy).
    await _delete_redis_buffer_keys_for_conversations(conv_ids)
    return report


async def purge_old_audio_files(retention_days: int | None = None) -> dict:
    """Retención de notas de voz en disco: borra los FICHEROS de audio más
    antiguos que la ventana y marca `media_purged_at`. Conserva la transcripción
    (texto) — el mensaje sigue existiendo, solo desaparece el audio pesado.
    """
    from app.db.session import db_session
    from app.models.message import Message

    days = retention_days if retention_days is not None else settings.AUDIO_RETENTION_DAYS
    if not days or days <= 0:
        return {"purged": 0, "skipped": "retención de audio desactivada"}
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    purged = 0
    async with db_session() as db:
        rows = (
            await db.execute(
                select(Message).where(
                    Message.audio_url.is_not(None),
                    Message.audio_url.like("/audios/%"),  # solo ficheros locales
                    Message.media_purged_at.is_(None),
                    Message.created_at < cutoff,
                )
            )
        ).scalars().all()
        for m in rows:
            path = _disk_path_for_served_url(m.audio_url)
            _unlink_quiet(path)  # aunque el fichero ya no esté, marcamos purgado
            m.audio_url = None  # el panel ya no intentará reproducir un fichero ausente
            m.media_purged_at = datetime.now(timezone.utc)
            purged += 1
        if purged:
            await db.commit()

    logger.info("audio_retention.purged", count=purged, retention_days=days)
    return {"purged": purged, "retention_days": days}


async def purge_old_media_files(retention_days: int | None = None) -> dict:
    """Retención de los ADJUNTOS en disco (imágenes, vídeos, documentos).

    Mismo criterio que las notas de voz: se borra el FICHERO y se marca
    `media_purged_at`, pero la fila del mensaje se queda. En la bandeja el
    histórico sigue diciendo "imagen recibida" con su fecha.

    Hasta ahora solo se purgaba el audio: la carpeta `uploads/` (adjuntos del
    cliente y los que envía el operador desde el panel) crecía sin límite en el
    volumen del servidor.
    """
    from app.db.session import db_session
    from app.models.message import Message

    days = retention_days if retention_days is not None else settings.MEDIA_RETENTION_DAYS
    if not days or days <= 0:
        return {"purged": 0, "skipped": "retención de adjuntos desactivada"}
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    purged = 0
    freed_bytes = 0
    async with db_session() as db:
        rows = (
            await db.execute(
                select(Message).where(
                    Message.media_url.is_not(None),
                    Message.media_url.like("/uploads/%"),  # solo ficheros locales
                    Message.created_at < cutoff,
                )
            )
        ).scalars().all()
        for m in rows:
            path = _disk_path_for_served_url(m.media_url)
            try:
                if path and os.path.isfile(path):
                    freed_bytes += os.path.getsize(path)
            except OSError:
                pass
            _unlink_quiet(path)  # aunque el fichero ya no esté, marcamos purgado
            m.media_url = None  # el panel ya no intentará descargar un fichero ausente
            m.media_purged_at = datetime.now(timezone.utc)
            purged += 1
        if purged:
            await db.commit()

    logger.info(
        "media_retention.purged",
        count=purged,
        retention_days=days,
        freed_bytes=freed_bytes,
    )
    return {"purged": purged, "retention_days": days, "freed_bytes": freed_bytes}
