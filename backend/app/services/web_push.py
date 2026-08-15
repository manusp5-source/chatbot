"""Web Push (notificaciones a la PWA del operador).

El operador instala el panel como PWA en el móvil y activa las notificaciones.
Cuando una conversación necesita atención (derivada a humano, o cliente
esperando respuesta), enviamos un Web Push firmado con VAPID a los endpoints
suscritos.

Claves VAPID: se autogeneran una vez y se guardan CIFRADAS en `credentials`
(`vapid_public_key` = clave pública en base64url para el navegador;
`vapid_private_key` = clave privada raw base64url para firmar con pywebpush).
No requieren intervención del operador ni servicios externos.
"""
from __future__ import annotations

import asyncio
import base64
import json
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.encryption import get_encryption_service
from app.core.logging import get_logger
from app.core.redis import get_redis
from app.db.session import db_session
from app.models.credential import Credential
from app.models.push_subscription import PushSubscription
from app.services.credentials import get_credential, invalidate_credential_cache

logger = get_logger(__name__)

_PUB_KEY = "vapid_public_key"
_PRIV_KEY = "vapid_private_key"


def vapid_subject() -> str:
    """Subject VAPID (contacto que exigen los push services en el JWT).

    Configurable con VAPID_SUBJECT (acepta "mailto:...", una URL https o un
    email a secas); si no está definido, se deriva del email del admin.
    Cambiarlo NO invalida las suscripciones existentes: lo que las liga al
    servidor son las claves VAPID, no el subject.
    """
    raw = (settings.VAPID_SUBJECT or "").strip()
    if not raw:
        raw = (settings.INITIAL_ADMIN_EMAIL or "").strip() or "admin@localhost"
    if raw.startswith(("mailto:", "https://", "http://")):
        return raw
    return f"mailto:{raw}"


# --------------------------------------------------------------------------
# VAPID keys
# --------------------------------------------------------------------------


def _generate_vapid_keypair() -> tuple[str, str]:
    """Genera un par VAPID (P-256). Devuelve (public_b64url, private_b64url).

    - public: punto sin comprimir (65 bytes) → applicationServerKey del navegador.
    - private: valor escalar (32 bytes) → lo acepta pywebpush/py_vapid.from_raw.
    """
    priv = ec.generate_private_key(ec.SECP256R1())
    priv_raw = priv.private_numbers().private_value.to_bytes(32, "big")
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    return b64(pub_raw), b64(priv_raw)


async def _set_credential(key: str, value: str) -> None:
    enc = get_encryption_service().encrypt(value)
    async with db_session() as db:
        row = (
            await db.execute(select(Credential).where(Credential.key == key))
        ).scalar_one_or_none()
        if row:
            row.value_encrypted = enc
        else:
            db.add(Credential(key=key, value_encrypted=enc))
        await db.commit()
    await invalidate_credential_cache(key)


async def ensure_vapid_keys() -> tuple[str, str]:
    """Devuelve (public, private), generándolas y persistiéndolas si faltan.

    Nota: en el caso (raro) de dos primeras generaciones simultáneas podría
    perderse una; con un único operador activando una vez no es un problema —
    bastaría re-activar. Las claves se generan UNA vez y luego persisten.
    """
    pub = await get_credential(_PUB_KEY)
    priv = await get_credential(_PRIV_KEY)
    if pub and priv:
        return pub, priv
    pub, priv = _generate_vapid_keypair()
    await _set_credential(_PUB_KEY, pub)
    await _set_credential(_PRIV_KEY, priv)
    logger.info("web_push.vapid.generated")
    return pub, priv


async def get_vapid_public_key() -> str:
    pub, _ = await ensure_vapid_keys()
    return pub


# --------------------------------------------------------------------------
# Suscripciones
# --------------------------------------------------------------------------


async def save_subscription(
    user_id: UUID, endpoint: str, p256dh: str, auth: str, user_agent: str | None
) -> None:
    """Upsert por endpoint: reusa la fila si el dispositivo ya estaba suscrito."""
    async with db_session() as db:
        row = (
            await db.execute(
                select(PushSubscription).where(PushSubscription.endpoint == endpoint)
            )
        ).scalar_one_or_none()
        if row:
            row.user_id = user_id
            row.p256dh = p256dh
            row.auth = auth
            row.user_agent = (user_agent or "")[:255] or None
        else:
            db.add(
                PushSubscription(
                    user_id=user_id,
                    endpoint=endpoint,
                    p256dh=p256dh,
                    auth=auth,
                    user_agent=(user_agent or "")[:255] or None,
                )
            )
        await db.commit()


async def delete_subscription(endpoint: str) -> None:
    async with db_session() as db:
        await db.execute(
            delete(PushSubscription).where(PushSubscription.endpoint == endpoint)
        )
        await db.commit()


# --------------------------------------------------------------------------
# Envío
# --------------------------------------------------------------------------


def _send_one(sub_info: dict, payload: str, private_key: str) -> int | None:
    """Envía un push (bloqueante). Devuelve el status HTTP del endpoint expirado
    (404/410) para que el caller borre la suscripción, o None si fue OK.
    Re-lanza otras excepciones para loguearlas."""
    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info=sub_info,
            data=payload,
            vapid_private_key=private_key,
            vapid_claims={"sub": vapid_subject()},
            timeout=10,
        )
        return None
    except WebPushException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (404, 410):
            return status
        raise


async def send_to_all(title: str, body: str, url: str | None = None) -> None:
    """Envía el push a todas las suscripciones. Limpia las expiradas (404/410)."""
    _, private_key = await ensure_vapid_keys()
    payload = json.dumps({"title": title, "body": body, "url": url or "/"})

    async with db_session() as db:
        subs = (await db.execute(select(PushSubscription))).scalars().all()
        sub_data = [
            (
                s.endpoint,
                {"endpoint": s.endpoint, "keys": {"p256dh": s.p256dh, "auth": s.auth}},
            )
            for s in subs
        ]

    expired: list[str] = []
    for endpoint, info in sub_data:
        try:
            status = await asyncio.to_thread(_send_one, info, payload, private_key)
            if status in (404, 410):
                expired.append(endpoint)
        except Exception as e:  # noqa: BLE001 - best-effort, nunca rompemos
            logger.warning("web_push.send.error", endpoint=endpoint[:60], error=str(e)[:120])

    for endpoint in expired:
        await delete_subscription(endpoint)
    if expired:
        logger.info("web_push.cleanup.expired", count=len(expired))


# --------------------------------------------------------------------------
# Disparadores (encolan la tarea Celery; no bloquean el caller)
# --------------------------------------------------------------------------


def _enqueue(title: str, body: str, url: str | None) -> None:
    # Import perezoso para evitar ciclo service<->task.
    from app.tasks.web_push_send import send_web_push

    send_web_push.delay(title, body, url)


async def notify(title: str, body: str, url: str | None = None) -> None:
    """Notifica sin cooldown (p.ej. derivación a humano: ocurre una vez)."""
    try:
        _enqueue(title, body, url)
    except Exception as e:  # noqa: BLE001
        logger.warning("web_push.notify.enqueue_error", error=str(e)[:120])


async def notify_pending(
    conversation_id: str, title: str, body: str, url: str | None = None, cooldown_seconds: int = 300
) -> None:
    """Notifica que una conversación está pendiente, con cooldown por conversación
    (evita un push por cada mensaje cuando el cliente escribe varios seguidos)."""
    try:
        r = get_redis()
        # SET NX EX: solo el primero dentro de la ventana pasa.
        ok = await r.set(f"push:pending:{conversation_id}", "1", nx=True, ex=cooldown_seconds)
        if not ok:
            return
        _enqueue(title, body, url)
    except Exception as e:  # noqa: BLE001
        logger.warning("web_push.notify_pending.error", error=str(e)[:120])
