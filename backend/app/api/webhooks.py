import json
from typing import cast

from fastapi import APIRouter, HTTPException, Query, Request, Response, status

from app.core.config import settings
from app.core.logging import get_logger
from app.core.ratelimit import make_limiter
from app.providers.instagram import get_instagram_provider
from app.providers.whatsapp import (
    PROVIDER_META,
    PROVIDER_YCLOUD,
    MetaCloudProvider,
    YCloudProvider,
)
from app.services.runtime_logs import push_runtime_log
from app.services.conversation import (
    mark_agent_queued,
    store_incoming,
    store_outgoing_instagram_echo,
)
from app.tasks.process_message import process_message as task_process

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = get_logger(__name__)
limiter = make_limiter()

MAX_WEBHOOK_BYTES = 1 * 1024 * 1024  # 1 MB — los payloads de WhatsApp son pequeños


def get_ycloud_provider() -> YCloudProvider:
    """Instancia de YCloud para SU endpoint. Ver el comentario de abajo."""
    from app.providers.whatsapp.selector import provider_by_name

    return cast(YCloudProvider, provider_by_name(PROVIDER_YCLOUD))


def get_meta_provider() -> MetaCloudProvider:
    """Instancia de Meta para SU endpoint."""
    from app.providers.whatsapp.selector import provider_by_name

    return cast(MetaCloudProvider, provider_by_name(PROVIDER_META))


async def _enqueue_agent(msg_id, canal: str) -> bool:
    """Encola el runtime del agente para un mensaje ya guardado.

    Antes esto era un `task_process.delay(...)` a pelo. Si el broker no
    respondía, la excepción rompía el BUCLE del webhook: el mensaje quedaba en
    la BD sin procesar para siempre (su reintento se descartaba por duplicado) y
    los mensajes siguientes del mismo envío ni se llegaban a guardar.

    Ahora el fallo se aísla por mensaje y se deja VISIBLE en Monitorización. El
    mensaje conserva `extra.agent_queued = False`, que es lo que permite que el
    reintento del proveedor lo reencole en vez de tirarlo (ver `store_incoming`).
    """
    try:
        task_process.delay(str(msg_id))
    except Exception as e:  # noqa: BLE001 — un mensaje no puede tumbar el lote
        logger.error("webhook.enqueue_failed", canal=canal, error=str(e))
        await push_runtime_log(
            level="error",
            event="message.enqueue_failed",
            message=(
                "El mensaje se ha guardado pero NO se ha podido encolar para el "
                "agente (¿broker caído?). Queda pendiente de reintento."
            ),
            message_id=str(msg_id),
            channel=canal,
        )
        return False
    # Marca "ya encolado": a partir de aquí un duplicado sí se descarta.
    await mark_agent_queued(msg_id)
    return True


def _estado_meta(st: dict) -> dict:
    """Estado de la Cloud API de Meta → la forma común que usa el anotador.

    Meta mete el motivo en una lista `errors` con `code`, `title`, `message` y
    un `error_data.details` que suele ser lo más explicativo de los cuatro.
    YCloud lo manda plano en `errorCode`/`errorMessage`.
    """
    errores = st.get("errors") or []
    primero = errores[0] if errores and isinstance(errores[0], dict) else {}
    detalles = ""
    datos = primero.get("error_data")
    if isinstance(datos, dict):
        detalles = str(datos.get("details") or "")
    return {
        "id": str(st.get("id") or ""),
        "wamid": "",
        "status": str(st.get("status") or ""),
        "error_code": str(primero.get("code") or ""),
        "error_message": detalles or str(primero.get("title") or primero.get("message") or ""),
    }


def _motivo_del_fallo(error_code: str, error_message: str) -> str:
    """Texto corto y en cristiano del porqué WhatsApp no ha entregado algo."""
    partes = [p for p in (error_message.strip(), f"código {error_code}" if error_code else "") if p]
    return " · ".join(partes) or "sin motivo"


async def _anotar_estados_de_entrega(estados: list[dict], *, origen: str) -> int:
    """Guarda en cada mensaje qué ha pasado con su entrega, y avisa si falló.

    Antes esto solo existía para Meta y solo para los fallidos, y acababa en
    los logs en vivo: un buffer de 1000 líneas en Redis que se pierde al
    reiniciar. En la bandeja el mensaje seguía apareciendo como enviado, así
    que la operadora daba por contestado a un cliente que no había recibido
    nada. Ahora el estado se pega al mensaje y se ve en su globo.

    Devuelve cuántos estados se han podido anotar (los de mensajes que no son
    nuestros se ignoran en silencio: pueden ser de otra instalación).
    """
    from app.services.delivery_status import FALLIDO, registrar_estado_entrega, traducir_estado

    anotados = 0
    for st in estados:
        motivo = _motivo_del_fallo(st.get("error_code") or "", st.get("error_message") or "")
        # El envío guarda el id de YCloud O el wamid de Meta según lo que
        # devolviera la API: se prueban los dos.
        for pmid in (st.get("id") or "", st.get("wamid") or ""):
            if pmid and await registrar_estado_entrega(
                pmid, st.get("status") or "", detalle=motivo
            ):
                anotados += 1
                break
        if traducir_estado(st.get("status")) == FALLIDO:
            await push_runtime_log(
                level="error",
                event="whatsapp.delivery.failed",
                message=f"WhatsApp no ha podido entregar un mensaje: {motivo}",
                provider_message_id=str(st.get("id") or st.get("wamid") or ""),
                error_code=str(st.get("error_code") or ""),
                origen=origen,
            )
    return anotados


async def _read_webhook_body(request: Request) -> bytes:
    """Cuerpo del webhook con tope de tamaño (defensa contra body bombs)."""
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Payload demasiado grande")
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Payload demasiado grande")
    return body


@router.post("/ycloud")
@limiter.limit("120/minute")
async def ycloud_webhook(request: Request) -> dict:
    body = await _read_webhook_body(request)

    headers = {k.lower(): v for k, v in request.headers.items()}
    # Explícitamente YCloud, no el proveedor "activo": esta URL es la que se
    # pega en el panel de YCloud, así que quien llama aquí es YCloud y solo él.
    # Meta tiene la suya (`/webhooks/whatsapp/meta`), y así ninguno de los dos
    # tiene que adivinar quién le está escribiendo.
    provider = get_ycloud_provider()

    if not await provider.verify_webhook_signature(headers, body):
        # El detalle ya lo loguea verify_webhook_signature en runtime_logs
        # (no_secret / no_header / malformed / mismatch). Aqui solo HTTP 401.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Firma inválida")

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Payload no JSON")

    incomings = provider.parse_webhook(payload)
    logger.info("webhook.ycloud", parsed=len(incomings))

    enqueued = 0
    for incoming in incomings:
        msg_id = await store_incoming(incoming)
        if msg_id is not None and await _enqueue_agent(msg_id, "whatsapp"):
            enqueued += 1

    # `whatsapp.message.updated`: lo que pasa con lo que enviamos nosotros.
    # Este evento llegaba y se tiraba entero.
    anotados = await _anotar_estados_de_entrega(
        provider.parse_statuses(payload), origen="ycloud"
    )

    return {
        "ok": True,
        "processed": len(incomings),
        "enqueued": enqueued,
        "statuses": anotados,
    }


# ---------- WhatsApp por la API Cloud oficial de Meta ----------
#
# Endpoint APARTE del de YCloud a propósito. Son dos proveedores con dos
# panales distintos, dos formas de firmar y —solo Meta— un handshake previo:
# una URL por proveedor es lo que hace que quien recibe un mensaje sepa sin
# ambigüedad de quién viene, y que cambiar de proveedor sea cambiar de URL en
# vez de rezar para que el payload se parezca al del otro.


@router.get("/whatsapp/meta")
async def meta_whatsapp_verify(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
) -> Response:
    """Handshake inicial de Meta: hay que devolver el challenge tal cual.

    Meta llama aquí al guardar la URL del webhook en su panel. Si la palabra
    de verificación coincide con la guardada, se devuelve el challenge en
    texto plano; si no, 403 y Meta no deja guardar la URL.
    """
    meta = get_meta_provider()
    challenge = await meta.verify_webhook_handshake(
        hub_mode, hub_verify_token, hub_challenge
    )
    if challenge is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "La palabra de verificación no coincide con la guardada en Conexiones → WhatsApp",
        )
    return Response(content=challenge, media_type="text/plain")


@router.post("/whatsapp/meta")
@limiter.limit("120/minute")
async def meta_whatsapp_webhook(request: Request) -> dict:
    body = await _read_webhook_body(request)
    headers = {k.lower(): v for k, v in request.headers.items()}
    meta = get_meta_provider()

    if not await meta.verify_webhook_signature(headers, body):
        # El motivo ya queda en Monitorización (no_secret / no_header /
        # mismatch); aquí solo el 401.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Firma inválida")

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Payload no JSON")

    incomings = meta.parse_webhook(payload)
    logger.info("webhook.whatsapp.meta", parsed=len(incomings))

    enqueued = 0
    for incoming in incomings:
        msg_id = await store_incoming(incoming)
        if msg_id is not None and await _enqueue_agent(msg_id, "whatsapp"):
            enqueued += 1

    # Estados de entrega. Ya no solo los fallidos ni solo a Monitorización: el
    # estado se pega al propio mensaje (`extra.delivery_status`) para que en la
    # bandeja se distinga "enviado" de "el cliente no lo ha recibido". Un
    # mensaje que Meta rechaza —número inexistente, cliente que bloqueó a la
    # empresa, ventana cerrada, plantilla pausada— se daba por contestado.
    estados = [_estado_meta(s) for s in meta.parse_statuses(payload)]
    anotados = await _anotar_estados_de_entrega(estados, origen="meta")
    fallidos = sum(1 for e in estados if e["status"] == "failed")

    # Meta espera 200 siempre que la firma sea válida, aunque el evento no sea
    # de mensajes. Si devolvemos 4xx/5xx reintenta sin parar.
    return {
        "ok": True,
        "processed": len(incomings),
        "enqueued": enqueued,
        "failed_statuses": fallidos,
        "statuses": anotados,
    }


# ---------- Instagram DM (F5) ----------


@router.get("/instagram")
async def instagram_verify(
    request: Request,
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
) -> Response:
    """Handshake inicial de Meta: debe devolver el challenge tal cual.

    Meta llama a este endpoint con ?hub.mode=subscribe&hub.verify_token=...
    cuando configuras el webhook. Si el verify_token coincide con el
    configurado en el Channel, devolvemos el challenge en plano. Si no, 403.
    """
    ig = get_instagram_provider()
    challenge = await ig.verify_webhook_handshake(hub_mode, hub_verify_token, hub_challenge)
    if challenge is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "verify_token incorrecto")
    return Response(content=challenge, media_type="text/plain")


@router.post("/instagram")
@limiter.limit("120/minute")
async def instagram_webhook(request: Request) -> dict:
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Payload demasiado grande")
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Payload demasiado grande")

    headers = {k.lower(): v for k, v in request.headers.items()}
    provider = get_instagram_provider()
    if not await provider.verify_webhook_signature(headers, body):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Firma inválida")

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Payload no JSON")

    incomings = provider.parse_webhook(payload)
    logger.info("webhook.instagram", parsed=len(incomings))

    # Diagnóstico TEMPORAL (visible en "Logs en vivo"): qué estructura manda
    # realmente Meta para cada mensaje IG. Sirve para entender por qué las notas
    # de voz no se capturan. NO incluye contenido: solo tipos de adjunto y nombres
    # de campos. Apagado por defecto (settings.IG_WEBHOOK_DEBUG); se enciende solo
    # para depurar.
    try:
        if settings.IG_WEBHOOK_DEBUG:
            for _entry in payload.get("entry") or []:
                for _ev in _entry.get("messaging") or []:
                    _m = _ev.get("message") or {}
                    if _m.get("is_echo"):
                        continue
                    _atts = _m.get("attachments") or []
                    # Solo registramos lo interesante: mensajes con adjuntos o sin
                    # texto (un texto normal no aporta nada al diagnóstico).
                    if _atts or not _m.get("text"):
                        await push_runtime_log(
                            level="info",
                            event="instagram.msg.debug",
                            message="Diagnóstico de mensaje IG (estructura)",
                            has_text=bool(_m.get("text")),
                            attachment_types=[a.get("type") for a in _atts],
                            attachment_payload_keys=[
                                sorted((a.get("payload") or {}).keys()) for a in _atts
                            ],
                            message_keys=sorted(_m.keys()),
                        )
    except Exception:
        pass

    # Visibilidad en el panel "Logs en vivo": confirmamos cuándo entra una nota
    # de voz de Instagram (el administrador no puede revisar los logs raw del contenedor).
    audio_count = sum(1 for m in incomings if m.audio_url)
    if audio_count:
        await push_runtime_log(
            level="info",
            event="instagram.audio.received",
            message=f"{audio_count} nota(s) de voz de Instagram recibida(s)",
            count=audio_count,
        )

    for incoming in incomings:
        # Enriquecer el contacto de Instagram con su @usuario/nombre real (el
        # webhook solo trae el PSID numérico). Best-effort + cacheado en Redis.
        if incoming.customer_name is None and incoming.from_phone.startswith("ig:"):
            ig_name, ig_handle = await _resolve_ig_profile(provider, incoming.from_phone)
            # Nombre real arriba; si no hay nombre público, usamos el @usuario.
            incoming.customer_name = ig_name or ig_handle
            incoming.customer_handle = ig_handle
        msg_id = await store_incoming(incoming)
        if msg_id is not None:
            await _enqueue_agent(msg_id, "instagram_dm")

    # Ecos: respuestas salientes de la cuenta (incluidas las escritas a mano
    # desde la app de Instagram). Se reflejan en el panel como saliente; NO se
    # encola el agente. El dedupe (no duplicar lo enviado desde el panel/el
    # agente) lo hace store_outgoing_instagram_echo por provider_message_id.
    echoes = provider.parse_echoes(payload)
    echoes_stored = 0
    for echo in echoes:
        if await store_outgoing_instagram_echo(
            echo.provider_message_id, echo.customer_id, echo.text
        ):
            echoes_stored += 1
    if echoes:
        logger.info("webhook.instagram.echoes", parsed=len(echoes), stored=echoes_stored)

    # Meta espera 200 OK siempre que firma sea válida (incluso para eventos no
    # de mensajes). Si devolvemos 4xx/5xx, reintentará indefinidamente.
    return {"ok": True, "processed": len(incomings), "echoes": echoes_stored}


async def _resolve_ig_profile(provider, from_phone: str) -> tuple[str | None, str | None]:
    """(nombre_real, @handle) de Instagram con caché en Redis, para no llamar a
    la Graph API en cada mensaje del mismo contacto. Best-effort."""
    import json

    from app.core.redis import get_redis

    psid = from_phone[3:] if from_phone.startswith("ig:") else from_phone
    redis = get_redis()
    # Clave nueva (v2): la antigua guardaba solo un string ("@usuario").
    key = f"ig:profile:v2:{psid}"
    try:
        cached = await redis.get(key)
        if cached:
            raw = cached if isinstance(cached, str) else cached.decode()
            obj = json.loads(raw)
            return obj.get("name"), obj.get("handle")
    except Exception:
        pass
    name, username = await provider.get_user_profile(psid)
    handle = f"@{username}" if username else None
    if name or handle:
        try:
            await redis.setex(
                key, 7 * 24 * 3600, json.dumps({"name": name, "handle": handle})
            )
        except Exception:
            pass
    return name, handle
