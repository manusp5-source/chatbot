"""Provider Instagram DM vía Meta Graph API (F5).

Configuración por Channel:
  Channel.config = {
    "page_id":         "<id pagina>",
    "page_access_token": "<token largo de Meta>",
    "verify_token":    "<elegido por nosotros, sirve para handshake GET>",
    "app_secret":      "<de la app Meta — valida la firma del POST>",
  }

Flujo:
  - Meta valida el webhook con GET ?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...
  - Eventos llegan por POST con header X-Hub-Signature-256: sha256=<hex_hmac>
    sobre el body raw, usando app_secret.
  - Para enviar: POST https://graph.facebook.com/v18.0/me/messages
    con ?access_token=<page_access_token>
    body: {recipient: {id: psid}, messaging_type: "RESPONSE", message: {text: "..."}}

Docs:
  https://developers.facebook.com/docs/messenger-platform/instagram/get-started
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.core.url_guard import resolves_to_private
from sqlalchemy import select

from app.core.logging import get_logger
from app.db.session import db_session
from app.models.channel import Channel, ChannelType
from app.providers.whatsapp.base import IncomingMessage
from app.services.channel_secrets import channel_config
from app.services.runtime_logs import push_runtime_log

logger = get_logger(__name__)

# API antigua (vía Facebook Page) — fallback si el administrador no tiene Business Login
META_GRAPH_VERSION = "v18.0"
META_GRAPH_BASE = f"https://graph.facebook.com/{META_GRAPH_VERSION}"

# API nueva (Instagram with Instagram Login, sin Facebook Page)
IG_GRAPH_VERSION = "v22.0"
IG_GRAPH_BASE = f"https://graph.instagram.com/{IG_GRAPH_VERSION}"

# Tipos de adjunto que manda Meta → categoría nuestra (MediaKind). El audio va
# aparte (tiene pipeline propio de transcripción). Los tipos que no están aquí
# se ignoran a propósito: preferimos un mensaje marcado "other" a inventarnos
# una categoría. `share` y `story_mention` traen la imagen/vídeo de la historia
# o publicación citada; la categoría definitiva se corrige con el MIME real al
# descargar (una historia puede ser vídeo).
_MEDIA_KIND_BY_ATT_TYPE: dict[str, str] = {
    "image": "image",
    "photo": "image",
    "sticker": "sticker",
    "video": "video",
    "reel": "video",
    "ig_reel": "video",
    "file": "document",
    "share": "image",
    "story_mention": "image",
}

# Hosts de Meta desde los que aceptamos descargar adjuntos. Compartido por
# audio y resto de media (defensa anti-SSRF, ver _download_from_meta).
_META_MEDIA_HOSTS = (
    "fbcdn.net",
    "fbsbx.com",
    "cdninstagram.com",
    "facebook.com",
    "instagram.com",
)


@dataclass
class IGCredentials:
    page_id: str  # En API nueva = Instagram User ID
    page_access_token: str  # En API nueva = IG access_token
    verify_token: str
    app_secret: str
    is_business_login: bool = False  # True si viene del flujo OAuth Business


@dataclass
class OutgoingEcho:
    """Un mensaje SALIENTE de la cuenta de negocio que Meta nos reenvía como
    "eco" (is_echo). Incluye lo que escribimos desde el panel/el agente y
    TAMBIÉN lo que se contesta a mano desde la propia app de Instagram.

    `customer_id` es el destinatario (el cliente), ya con prefijo `ig:` para
    casar con `Contact.telefono`.
    """

    provider_message_id: str  # message.mid (coincide con el id que devuelve Send API)
    customer_id: str  # "ig:<psid_del_cliente>"
    text: str | None


async def get_ig_credentials() -> IGCredentials | None:
    """Devuelve las credenciales para usar la API de Instagram.

    Orden de prioridad:
      1. Instagram Business Login (ExternalAPI provider=instagram_business)
         + verify_token y app_secret del Channel.
      2. Token "Page" clásico del Channel.config (legacy, requiere FB Page).

    El verify_token y el app_secret SIEMPRE viven en el Channel porque son del
    webhook, no del OAuth. El access_token y user_id pueden venir del OAuth
    (preferido) o del Channel (legacy).

    Los tres secretos (app_secret, verify_token, page_access_token) van
    cifrados en `credentials_encrypted`; `channel_config` los descifra y los
    junta con el resto de la config.
    """
    async with db_session() as db:
        # El más antiguo, y solo uno: con dos canales de Instagram activos esto
        # era un MultipleResultsFound que dejaba el canal mudo. Mismo criterio
        # que la provisión del panel, para escribir y leer la misma fila.
        ch = (
            await db.execute(
                select(Channel)
                .where(
                    Channel.type == ChannelType.instagram_dm,
                    Channel.enabled.is_(True),
                )
                .order_by(Channel.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
    if not ch:
        return None
    cfg = channel_config(ch)
    verify_token = cfg.get("verify_token")
    app_secret = cfg.get("app_secret")
    if not (verify_token and app_secret):
        return None

    # 1) Intento: Business Login (OAuth)
    try:
        from app.services.instagram_oauth import get_access_token, get_instagram_user_id
        access_token = await get_access_token()
        user_id = await get_instagram_user_id()
        if access_token and user_id:
            return IGCredentials(
                page_id=user_id,
                page_access_token=access_token,
                verify_token=verify_token,
                app_secret=app_secret,
                is_business_login=True,
            )
    except Exception as e:
        logger.warning("ig.business_login.read_failed", error=str(e))

    # 2) Fallback: token clásico tipo Page
    page_id = cfg.get("page_id")
    page_access_token = cfg.get("page_access_token")
    if not (page_id and page_access_token):
        return None
    return IGCredentials(
        page_id=page_id,
        page_access_token=page_access_token,
        verify_token=verify_token,
        app_secret=app_secret,
        is_business_login=False,
    )


class InstagramProvider:
    async def verify_webhook_handshake(
        self, mode: str | None, token: str | None, challenge: str | None
    ) -> str | None:
        """Para el GET de verificación inicial de Meta.

        Devuelve el challenge si todo OK; None si rechaza.
        """
        if mode != "subscribe":
            return None
        creds = await get_ig_credentials()
        if not creds:
            await push_runtime_log(
                level="warn",
                event="instagram.verify.no_config",
                message="Llegó verificación de Meta pero no hay canal IG configurado",
            )
            return None
        if not hmac.compare_digest(creds.verify_token, token or ""):
            await push_runtime_log(
                level="warn",
                event="instagram.verify.bad_token",
                message="verify_token de Meta no coincide con el del canal",
            )
            return None
        return challenge

    async def verify_webhook_signature(
        self, headers: dict[str, str], body: bytes
    ) -> bool:
        creds = await get_ig_credentials()
        if not creds:
            return False
        sig = (
            headers.get("x-hub-signature-256")
            or headers.get("X-Hub-Signature-256")
        )
        if not sig or not sig.startswith("sha256="):
            await push_runtime_log(
                level="warn",
                event="instagram.signature.no_header",
                message="Webhook IG sin X-Hub-Signature-256",
            )
            return False
        expected = hmac.new(creds.app_secret.encode(), body, hashlib.sha256).hexdigest()
        ok = hmac.compare_digest(sig.split("=", 1)[1], expected)
        if not ok:
            await push_runtime_log(
                level="error",
                event="instagram.signature.mismatch",
                message="HMAC IG no coincide. Revisa app_secret del canal.",
            )
        return ok

    def parse_webhook(self, payload: dict) -> list[IncomingMessage]:
        """Parsea el payload de Meta Graph webhook.

        Formato típico:
        {
          "object": "instagram",
          "entry": [{
            "id": "<page_id>",
            "messaging": [{
              "sender": {"id": "<psid>"},
              "recipient": {"id": "<page_id>"},
              "timestamp": 1700000000000,
              "message": {"mid": "...", "text": "hola"}
            }]
          }]
        }
        """
        results: list[IncomingMessage] = []
        if payload.get("object") not in ("instagram", "page"):
            return results
        for entry in payload.get("entry") or []:
            page_id = str(entry.get("id") or "")
            for ev in entry.get("messaging") or []:
                msg = ev.get("message") or {}
                if msg.get("is_echo"):
                    # echo = mensaje saliente de la cuenta. No es entrante: lo
                    # refleja parse_echoes() (espejo del panel), no aquí.
                    continue
                sender_psid = str((ev.get("sender") or {}).get("id") or "")
                wamid = str(msg.get("mid") or "")
                text = msg.get("text")
                if not sender_psid or not wamid:
                    continue
                # Notas de voz: Instagram las manda como adjuntos type=audio (a
                # veces "voice"). Reusamos el MISMO pipeline que WhatsApp: con
                # audio_url, store_incoming lo guarda y el procesado encola
                # transcribe_audio (Whisper). Capturamos el audio AUNQUE venga
                # texto: el mensaje guardará ambos (contenido + audio_url) y la
                # transcripción se añade después, así no se pierde ninguna nota
                # de voz por traer también una transcripción/caption de Meta.
                attachments = msg.get("attachments") or []
                audio_url = None
                audio_mime = None
                media_kind = None
                media_url = None
                media_mime = None
                for att in attachments:
                    att_type = (att.get("type") or "").lower()
                    # OJO: variable propia. Antes se llamaba `payload` y pisaba
                    # el parámetro de la función dentro del bucle.
                    att_payload = att.get("payload") or {}
                    if att_type in ("audio", "voice") or att_type.startswith("audio"):
                        audio_url = att_payload.get("url")
                        # Meta no suele incluir el mime en el adjunto, pero si
                        # llega lo aprovechamos (si no, se infiere al descargar).
                        audio_mime = att_payload.get("mime_type") or att_payload.get("mime")
                        if audio_url:
                            break
                    elif media_url is None:
                        # Imagen, vídeo, fichero, historia mencionada… Nos
                        # quedamos con el PRIMERO que traiga URL. Sin esto el
                        # mensaje se guardaba vacío (burbuja en blanco en la
                        # bandeja) y el agente ni se enteraba de que había algo.
                        kind = _MEDIA_KIND_BY_ATT_TYPE.get(att_type)
                        url = att_payload.get("url")
                        if kind and url:
                            media_kind = kind
                            media_url = url
                            media_mime = att_payload.get("mime_type") or att_payload.get("mime")
                # Diagnóstico: registramos los tipos de adjunto que manda Meta
                # para poder confirmar en producción qué llega realmente (no se
                # puede probar contra Meta en local). No incluye datos sensibles.
                if attachments:
                    logger.info(
                        "instagram.attachments",
                        types=[(a.get("type") or "?") for a in attachments],
                        has_text=bool(text),
                        audio_detected=bool(audio_url),
                        media_detected=media_kind or None,
                    )
                if audio_url:
                    message_type = "audio"
                elif media_kind:
                    # image | video | document | sticker. El texto, si viene, es
                    # el pie del adjunto y se guarda igual en `text`.
                    message_type = media_kind
                elif text:
                    message_type = "text"
                else:
                    # Adjunto sin URL o tipo que no sabemos representar.
                    message_type = "other"
                # No tenemos un teléfono real → usamos sender_psid como identificador
                # único del contacto. El prefijo "ig:" lo añadirá store_incoming.
                results.append(
                    IncomingMessage(
                        provider_message_id=wamid,
                        from_phone=f"ig:{sender_psid}",
                        to_phone=f"ig:{page_id}" if page_id else "ig:page",
                        message_type=message_type,
                        text=text,
                        audio_url=audio_url,
                        audio_mime=audio_mime,
                        media_kind=media_kind,
                        media_url=media_url,
                        media_mime=media_mime,
                        customer_name=None,
                        raw=ev,
                    )
                )
        return results

    def parse_echoes(self, payload: dict) -> list[OutgoingEcho]:
        """Extrae los ecos (is_echo) del payload: mensajes salientes de la
        cuenta de negocio, incluidos los escritos a mano desde la app de
        Instagram. Se reflejan en el panel como saliente (rol=operator) para
        que el hilo sea un espejo fiel. El dedupe (no duplicar lo que enviamos
        desde el panel/el agente) se hace en store_outgoing_instagram_echo por
        provider_message_id.

        En un eco, `sender` es la cuenta y `recipient` es el cliente — al revés
        que en un entrante.
        """
        results: list[OutgoingEcho] = []
        if payload.get("object") not in ("instagram", "page"):
            return results
        for entry in payload.get("entry") or []:
            for ev in entry.get("messaging") or []:
                msg = ev.get("message") or {}
                if not msg.get("is_echo"):
                    continue
                mid = str(msg.get("mid") or "")
                recipient = str((ev.get("recipient") or {}).get("id") or "")
                if not mid or not recipient:
                    continue
                results.append(
                    OutgoingEcho(
                        provider_message_id=mid,
                        customer_id=f"ig:{recipient}",
                        text=msg.get("text"),
                    )
                )
        return results

    async def send_text(self, to_psid: str, body: str, *, human_agent: bool = False) -> str:
        """Envía texto al PSID del visitor.

        `to_psid` puede venir con prefijo `ig:` (lo nuestro) o crudo (de Meta).
        Normalizamos quitando el prefijo.

        Si tenemos Instagram Business Login → usamos graph.instagram.com.
        Si seguimos con token Page legacy → usamos graph.facebook.com.

        `human_agent`: cuando ya pasaron >24h desde el último mensaje del cliente
        (pero <7 días), Meta SOLO permite responder con la etiqueta HUMAN_AGENT
        (respuesta de un agente humano). Enviar texto normal fuera de la ventana
        de 24h viola la política de Instagram y arriesga restricciones de la
        cuenta. El llamante (channel_sender) decide cuándo activarla según la
        antigüedad del último mensaje entrante.
        """
        creds = await get_ig_credentials()
        if not creds:
            logger.error("instagram.send.no_credentials")
            return "noop"
        psid = to_psid[3:] if to_psid.startswith("ig:") else to_psid
        base = IG_GRAPH_BASE if creds.is_business_login else META_GRAPH_BASE
        url = f"{base}/me/messages"
        payload: dict = {
            "recipient": {"id": psid},
            "message": {"text": body},
        }
        if human_agent:
            # Etiqueta oficial de Meta para responder entre 24h y 7 días. La API
            # antigua (Page) exige messaging_type=MESSAGE_TAG junto al tag; la
            # nueva (Instagram Login) admite el tag directamente.
            payload["tag"] = "HUMAN_AGENT"
            if not creds.is_business_login:
                payload["messaging_type"] = "MESSAGE_TAG"
        elif not creds.is_business_login:
            # Solo la API antigua (Facebook Page) acepta messaging_type
            payload["messaging_type"] = "RESPONSE"
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                url,
                params={"access_token": creds.page_access_token},
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            if r.status_code >= 400:
                logger.error(
                    "instagram.send.error",
                    status=r.status_code,
                    body=r.text[:200],
                    via="business_login" if creds.is_business_login else "page",
                )
                r.raise_for_status()
            data = r.json()
        return str(data.get("message_id") or "")

    async def get_user_profile(self, psid: str) -> tuple[str | None, str | None]:
        """Devuelve (nombre_real, username) del visitor que escribe, vía la User
        Profile API de Instagram. (None, None) si no se puede resolver (best-effort).

        El webhook solo trae el PSID/IGSID numérico; esto lo cambia por datos
        legibles para el inbox (nombre real + @usuario debajo). Requiere
        instagram_business_manage_messages.
        """
        creds = await get_ig_credentials()
        if not creds:
            return None, None
        psid = psid[3:] if psid.startswith("ig:") else psid
        base = IG_GRAPH_BASE if creds.is_business_login else META_GRAPH_BASE
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(
                    f"{base}/{psid}",
                    params={
                        "fields": "name,username",
                        "access_token": creds.page_access_token,
                    },
                )
                if r.status_code >= 400:
                    logger.warning(
                        "instagram.profile.error", status=r.status_code, body=r.text[:200]
                    )
                    return None, None
                data = r.json()
        except Exception as e:
            logger.warning("instagram.profile.failed", error=str(e))
            return None, None
        return (data.get("name") or None, data.get("username") or None)

    async def download_audio(self, url: str) -> tuple[bytes, str]:
        """Descarga una nota de voz de Instagram desde una URL de Meta.

        Devuelve (bytes, mime). Reusa el mismo formato de retorno que el
        provider de WhatsApp para que audio_processor sea idéntico.

        (Los detalles están en `_download_from_meta`, compartida con el resto
        de adjuntos.)
        """
        from app.services.agent_guardrails import MAX_AUDIO_BYTES

        return await self._download_from_meta(
            url,
            max_bytes=MAX_AUDIO_BYTES,
            event_prefix="instagram.audio",
            label="nota de voz",
            default_mime="audio/ogg",
        )

    async def download_media(self, url: str, *, max_bytes: int) -> tuple[bytes, str]:
        """Descarga un adjunto que NO es nota de voz (imagen, vídeo, fichero).

        Mismas defensas que el audio: lista blanca de hosts de Meta, anti-SSRF y
        tope de bytes. Devuelve (bytes, mime).
        """
        return await self._download_from_meta(
            url,
            max_bytes=max_bytes,
            event_prefix="instagram.media",
            label="adjunto",
            default_mime="application/octet-stream",
        )

    async def _download_from_meta(
        self,
        url: str,
        *,
        max_bytes: int,
        event_prefix: str,
        label: str,
        default_mime: str,
    ) -> tuple[bytes, str]:
        """Descarga un fichero de la CDN de Meta con todas las defensas.

        Detalles:
          - Las URLs lookaside.fbsbx.com suelen ser descargables directamente y
            redirigen a la CDN de Meta → seguimos redirects.
          - Si Meta responde 401/403 (token requerido), reintentamos UNA vez
            añadiendo ?access_token=<page_access_token>.
          - Topamos el tamaño con `max_bytes` (en el audio, DoS económico contra
            Whisper): si Content-Length lo supera, abortamos sin descargar; y
            cortamos el stream para no leer gigabytes aunque no venga
            Content-Length.
          - mime desde la cabecera Content-Type (fallback `default_mime`).
          - Lanza excepción ante cualquier fallo (el caller la maneja).
        """
        # Anti-SSRF: la URL viene del webhook (firmado por Meta), pero validamos
        # igual que el host sea de Meta/Instagram y NO resuelva a una IP interna,
        # para no exfiltrar el page token ni alcanzar la red interna si la URL
        # fuese maliciosa. (El payload está HMAC-verificado, esto es defensa extra.)
        _host = (urlparse(url).hostname or "").lower()
        # Diagnóstico: dejamos en "Logs en vivo" el host del que vamos a
        # descargar. Si algo falla, esto dice si es por host no permitido, por la
        # descarga (HTTP) o por lo que venga después.
        await push_runtime_log(
            level="info",
            event=f"{event_prefix}.download.start",
            message=f"Descargando {label} de Instagram",
            host=_host or "?",
        )
        if not (
            _host and any(_host == s or _host.endswith("." + s) for s in _META_MEDIA_HOSTS)
        ):
            logger.error(f"{event_prefix}.host_blocked", host=_host or "?")
            await push_runtime_log(
                level="error",
                event=f"{event_prefix}.host_blocked",
                message=f"Host del {label} de IG no permitido: {_host or '?'}. "
                "Si el host es legítimo, hay que añadirlo a la lista blanca del código.",
                host=_host or "?",
            )
            raise RuntimeError(f"URL de {label} IG con host no permitido")
        if await resolves_to_private(_host):
            logger.error(f"{event_prefix}.private_ip", host=_host)
            await push_runtime_log(
                level="error",
                event=f"{event_prefix}.private_ip",
                message=f"El host del {label} de IG ({_host}) no resuelve o "
                "apunta a una IP interna (revisar DNS del worker).",
                host=_host,
            )
            raise RuntimeError(f"URL de {label} IG apunta a una IP interna")

        async def _fetch(req_url: str) -> tuple[int, bytes, str]:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                async with client.stream("GET", req_url) as r:
                    status_code = r.status_code
                    if status_code >= 400:
                        # Drenamos para liberar la conexión antes de devolver.
                        await r.aread()
                        return status_code, b"", ""
                    # Corta temprano si el servidor declara un tamaño excesivo.
                    cl = r.headers.get("content-length")
                    if cl and int(cl) > max_bytes:
                        raise ValueError(
                            f"{label.capitalize()} IG demasiado grande: {cl} > {max_bytes}"
                        )
                    mime = r.headers.get("content-type", default_mime)
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in r.aiter_bytes():
                        total += len(chunk)
                        # Guarda aunque no haya Content-Length (lectura limitada).
                        if total > max_bytes:
                            raise ValueError(
                                f"{label.capitalize()} IG excede {max_bytes} bytes "
                                "durante la descarga"
                            )
                        chunks.append(chunk)
                    return status_code, b"".join(chunks), mime

        status_code, content, mime = await _fetch(url)
        if status_code in (401, 403):
            # Reintento con el token de la página (algunos media exigen auth).
            creds = await get_ig_credentials()
            if creds and creds.page_access_token:
                sep = "&" if "?" in url else "?"
                status_code, content, mime = await _fetch(
                    f"{url}{sep}access_token={creds.page_access_token}"
                )
        if status_code >= 400:
            logger.error(f"{event_prefix}.download_error", status=status_code)
            await push_runtime_log(
                level="error",
                event=f"{event_prefix}.download_error",
                message=f"Meta devolvió HTTP {status_code} al descargar el {label} "
                "(incluso reintentando con el token de la página).",
                status=status_code,
            )
            raise RuntimeError(f"No se pudo descargar {label} IG (HTTP {status_code})")
        await push_runtime_log(
            level="info",
            event=f"{event_prefix}.download.ok",
            message=f"{label.capitalize()} de IG descargado ({len(content)} bytes).",
            bytes=len(content),
        )
        return content, mime


_provider_singleton: InstagramProvider | None = None


def get_instagram_provider() -> InstagramProvider:
    global _provider_singleton
    if _provider_singleton is None:
        _provider_singleton = InstagramProvider()
    return _provider_singleton
