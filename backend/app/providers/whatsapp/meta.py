"""Implementación de la API Cloud oficial de Meta (WhatsApp Business Platform).

Es la alternativa a YCloud: aquí se habla con Meta directamente, sin
revendedor. El contrato es el mismo (`WhatsAppProvider`), así que el resto del
producto —bandeja, agente, difusiones, plantillas, notas de voz— funciona igual
con uno u otro. Lo elige cada instalación desde Conexiones y se guarda en el
canal; ver `app/providers/whatsapp/selector.py`.

Las cinco credenciales viven cifradas en la tabla `credentials` (mismo sitio y
mismo cifrado que las de YCloud) y se leen en cada llamada, así que cambiarlas
desde el panel aplica sin reiniciar:

  meta_wa_phone_number_id      Identificador del número (va en la URL de envío)
  meta_wa_business_account_id  WABA ID (hace falta para listar las plantillas)
  meta_wa_access_token         Token permanente de usuario del sistema
  meta_wa_app_secret           Firma los webhooks (X-Hub-Signature-256)
  meta_wa_verify_token         Palabra del handshake GET del webhook

DIFERENCIAS con YCloud que condicionan este fichero:

  1. Los adjuntos NO llegan con URL: llega un identificador de media y hay que
     pedir la URL aparte, y descargarla con el token. Por eso lo que se guarda
     en el mensaje es `meta-media:<id>` y la resolución ocurre al descargar
     (`download_audio` / `download_media`).
  2. La firma del webhook es HMAC-SHA256 del cuerpo CRUDO con el app secret, en
     la cabecera `X-Hub-Signature-256` con el prefijo `sha256=`. YCloud usa otra
     cabecera y firma `{timestamp}.{cuerpo}`, no el cuerpo solo.
  3. Meta pide además un handshake GET con `hub.challenge` que YCloud no tiene.
  4. Las plantillas se piden a la cuenta de negocio (WABA), no al número.

Documentación oficial de Meta. Todo lo de aquí está apoyado en ella, pero NO se
ha podido probar contra una cuenta real: esta instalación no tiene credenciales
de Meta. Lo que sí está probado es lo que construimos nosotros (el parseo, la
firma, el cuerpo de las peticiones y los errores) — ver
`tests/test_whatsapp_meta.py`.

OJO con las URLs: Meta migró la documentación de `/docs/whatsapp/cloud-api/…` a
`/documentation/business-messaging/whatsapp/…`. Conviven, pero algunas de las
antiguas ya dan error; abajo van las nuevas.

  https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages
  https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages/system
  https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages/unsupported
  https://developers.facebook.com/documentation/business-messaging/whatsapp/business-scoped-user-ids/
  https://developers.facebook.com/docs/graph-api/webhooks/getting-started
  https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages
  https://developers.facebook.com/docs/whatsapp/cloud-api/reference/media
  https://developers.facebook.com/docs/whatsapp/business-management-api/message-templates
  https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import socket
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlparse

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.providers.whatsapp.base import (
    WA_USER_PREFIX,
    IncomingMessage,
    MediaKind,
    TemplateListError,
    WhatsAppCredentialsUnreadableError,
    WhatsAppNotConfiguredError,
    WhatsAppProvider,
    build_header_component,
    normalize_phone,
    text_parameters,
)
from app.services.credentials import CredentialUnavailableError, get_credential
from app.services.runtime_logs import push_runtime_log

logger = get_logger(__name__)

# Versión de la Graph API. Meta mantiene cada versión unos dos años y luego la
# retira; se puede subir por entorno (`META_WA_GRAPH_VERSION`) sin tocar código
# el día que toque, que es exactamente el fallo que deja un canal mudo de
# repente. Meta NO declara en ninguna parte una versión mínima para la API
# Cloud: v25.0 es la que usan sus propios ejemplos hoy y está soportada hasta
# 2028. Ref: https://developers.facebook.com/docs/graph-api/changelog
GRAPH_VERSION = (getattr(settings, "META_WA_GRAPH_VERSION", "") or "v25.0").strip()
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

# Marca del identificador de media de Meta cuando viaja por los campos que en
# YCloud llevan una URL. No es una URL de verdad a propósito: si alguien
# intentara descargarla a pelo fallaría en vez de irse a un host cualquiera.
META_MEDIA_PREFIX = "meta-media:"

# Hosts desde los que Meta sirve los adjuntos. La URL la da la propia Graph API,
# pero se comprueba igualmente: una respuesta manipulada no puede hacer que el
# backend pida una URL interna con el token puesto.
_ALLOWED_DOWNLOAD_SUFFIXES = (
    "fbcdn.net",
    "fbsbx.com",
    "cdninstagram.com",
    "whatsapp.net",
    "facebook.com",
)

_TEMPLATES_MAX_PAGES = 20
_TEMPLATES_PAGE_SIZE = 100

# Tipos de mensaje que sabemos mapear a nuestro modelo. El resto entra como
# "other" con traza, nunca en silencio.
_MEDIA_TYPES = ("image", "video", "document", "sticker")

_pending_log_tasks: set[asyncio.Task] = set()


def _log_discard(event: str, message: str, **fields: object) -> None:
    """Deja traza de un evento entrante que descartamos.

    Mismo criterio que en YCloud: un `continue` mudo hacía que se perdieran
    clientes reales sin que quedara una línea en ningún sitio. Solo metadatos
    (banderas, tipos y NOMBRES de campo), nunca el contenido ni el teléfono.
    """
    logger.warning(event, **fields)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # fuera de un event loop (tests síncronos): basta el logger
    task = loop.create_task(
        push_runtime_log(level="warn", event=event, message=message, **fields)
    )
    _pending_log_tasks.add(task)
    task.add_done_callback(_pending_log_tasks.discard)


def _host_allowed(host: str) -> bool:
    h = host.lower()
    return any(h == s or h.endswith("." + s) for s in _ALLOWED_DOWNLOAD_SUFFIXES)


async def _resolves_to_private(host: str) -> bool:
    loop = asyncio.get_event_loop()
    try:
        infos = await loop.run_in_executor(None, socket.getaddrinfo, host, None)
    except socket.gaierror:
        return True  # si no resuelve, lo tratamos como inseguro
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
    return False


def _recipient_fields(destination: str) -> dict[str, str]:
    """Campo de destinatario: `to` (teléfono) O `recipient` (BSUID), nunca los dos.

    El BSUID NO va en el campo `to`: Meta añadió un campo aparte, `recipient`,
    y documenta que si van los dos, «`to` (phone number) will take
    precedence». Mandar ambos con un `to` inventado enviaría el mensaje a un
    número que no existe. Es la misma regla que en YCloud, que a fin de
    cuentas reenvía a Meta.

    El teléfono viaja solo con dígitos, que es como salen todos los ejemplos
    de Meta. El BSUID viaja literal y completo —código de país, punto y hasta
    128 caracteres, `ES.xxxx`— y el «padre» lleva otro formato, `ES.ENT.xxxx`;
    `recipient` acepta los dos.

    Ref: https://developers.facebook.com/documentation/business-messaging/whatsapp/business-scoped-user-ids/

    OJO — no verificado contra la API real: esta instalación no tiene
    credenciales de Meta con las que hacer la prueba.
    """
    if destination.startswith(WA_USER_PREFIX):
        return {"recipient": destination[len(WA_USER_PREFIX):]}
    return {"to": normalize_phone(destination).lstrip("+")}


class MetaCloudProvider(WhatsAppProvider):
    """Proveedor de WhatsApp contra la API Cloud de Meta."""

    # ------------------------------------------------------------------
    # Credenciales
    # ------------------------------------------------------------------

    async def _strict_credential(self, key: str) -> str | None:
        """Credencial en modo ESTRICTO: None si no está, LANZA si no se abre.

        Igual que en YCloud: "no configurada" se arregla escribiéndola y
        "guardada pero no se descifra" (típico tras cambiar `ENCRYPTION_KEY`)
        se arregla restaurando la clave. Confundirlos manda al operador a
        reintroducir credenciales que en realidad están bien.
        """
        try:
            return await get_credential(key, strict=True)
        except CredentialUnavailableError as exc:
            raise WhatsAppCredentialsUnreadableError(
                f"La credencial '{key}' está guardada pero no se puede descifrar "
                "(¿cambió ENCRYPTION_KEY?). No se ha enviado nada."
            ) from exc

    async def _phone_number_id(self) -> str | None:
        return await self._strict_credential("meta_wa_phone_number_id")

    async def _access_token(self) -> str | None:
        return await self._strict_credential("meta_wa_access_token")

    async def _waba_id(self) -> str | None:
        return await self._strict_credential("meta_wa_business_account_id")

    async def _app_secret(self) -> str | None:
        # NO estricto: verificar la firma tiene su propio camino de error (log
        # + False) y no queremos que una credencial ilegible tumbe el webhook.
        return await get_credential("meta_wa_app_secret")

    async def _verify_token(self) -> str | None:
        return await get_credential("meta_wa_verify_token")

    async def _require_credentials(self) -> tuple[str, str]:
        """Devuelve (phone_number_id, access_token) o LANZA. Nunca vacíos.

        Es lo que evita el «300 enviados» de una campaña que no mandó nada:
        antes de tocar la API, o están las credenciales o hay error.
        """
        pnid = await self._phone_number_id()
        token = await self._access_token()
        faltan = []
        if not pnid:
            faltan.append("el identificador del número (meta_wa_phone_number_id)")
        if not token:
            faltan.append("el token de acceso (meta_wa_access_token)")
        if faltan:
            raise WhatsAppNotConfiguredError(
                "WhatsApp no está configurado con la API de Meta: falta "
                + " y ".join(faltan)
                + ". Complétalo en Conexiones → WhatsApp. No se ha enviado nada."
            )
        return cast(str, pnid), cast(str, token)

    async def check_credentials(self) -> None:
        """Comprueba que se puede enviar. Lanza si no. No llama a la API."""
        await self._require_credentials()

    # ------------------------------------------------------------------
    # Webhook: handshake y firma
    # ------------------------------------------------------------------

    async def verify_webhook_handshake(
        self, mode: str | None, token: str | None, challenge: str | None
    ) -> str | None:
        """GET de verificación de Meta: devuelve el challenge o None.

        Meta llama con `?hub.mode=subscribe&hub.verify_token=…&hub.challenge=…`
        al guardar la URL del webhook y espera el challenge en texto plano.

        Ref: https://developers.facebook.com/docs/graph-api/webhooks/getting-started
        """
        if mode != "subscribe":
            return None
        expected = await self._verify_token()
        if not expected:
            await push_runtime_log(
                level="warn",
                event="meta_wa.verify.no_token",
                message=(
                    "Meta intentó verificar el webhook de WhatsApp pero no hay "
                    "palabra de verificación guardada en Conexiones → WhatsApp."
                ),
            )
            return None
        if not hmac.compare_digest(expected, token or ""):
            await push_runtime_log(
                level="warn",
                event="meta_wa.verify.bad_token",
                message=(
                    "La palabra de verificación que manda Meta no coincide con la "
                    "guardada. Tienen que ser idénticas en los dos sitios."
                ),
            )
            return None
        return challenge

    async def verify_webhook_signature(self, headers: dict[str, str], body: bytes) -> bool:
        """HMAC-SHA256 del cuerpo CRUDO con el app secret.

        Cabecera `X-Hub-Signature-256: sha256=<hex>`. Es distinta de la de
        YCloud en las tres cosas: nombre de la cabecera, prefijo, y qué se
        firma (Meta firma el cuerpo tal cual; YCloud firma
        `{timestamp}.{cuerpo}`).

        Ref: https://developers.facebook.com/docs/graph-api/webhooks/getting-started#event-notifications
        """
        secret = await self._app_secret()
        if not secret:
            logger.error("meta_wa.webhook.no_secret_configured")
            await push_runtime_log(
                level="error",
                event="webhook.signature.no_secret",
                message=(
                    "No hay clave secreta de la app de Meta guardada: sin ella no se "
                    "puede comprobar que los mensajes vengan de verdad de Meta."
                ),
            )
            return False
        sig = headers.get("x-hub-signature-256") or headers.get("X-Hub-Signature-256")
        if not sig or not sig.startswith("sha256="):
            logger.error("meta_wa.webhook.no_signature_header")
            await push_runtime_log(
                level="warn",
                event="webhook.signature.no_header",
                message="Webhook de WhatsApp sin cabecera X-Hub-Signature-256 (¿no es de Meta?)",
                headers_keys=",".join(list(headers.keys())[:8]),
            )
            return False
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        recibido = sig.split("=", 1)[1]
        ok = hmac.compare_digest(expected, recibido)
        if not ok:
            # Mismo criterio que YCloud: pistas útiles sin regalar el secreto
            # (solo su longitud y los primeros caracteres de los HMAC).
            await push_runtime_log(
                level="error",
                event="webhook.signature.mismatch",
                message=(
                    "La firma no coincide. Casi siempre es que la clave secreta de la "
                    "app guardada aquí no es la misma que la de la app en Meta."
                ),
                secret_len=len(secret),
                expected_hint=expected[:8] + "...",
                received_hint=recibido[:8] + "...",
                body_len=len(body),
            )
        return ok

    # ------------------------------------------------------------------
    # Webhook: mensajes entrantes
    # ------------------------------------------------------------------

    def parse_webhook(self, payload: dict) -> list[IncomingMessage]:
        """Normaliza el webhook de Meta a nuestros `IncomingMessage`.

        Forma del payload:
        {
          "object": "whatsapp_business_account",
          "entry": [{
            "id": "<waba id>",
            "changes": [{
              "field": "messages",
              "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "...", "phone_number_id": "..."},
                "contacts": [{"profile": {"name": "Ana"}, "wa_id": "34600..."}],
                "messages": [{"from": "34600...", "id": "wamid...",
                              "timestamp": "...", "type": "text",
                              "text": {"body": "hola"}}]
              }
            }]
          }]
        }

        Ref: https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/payload-examples
        """
        results: list[IncomingMessage] = []
        if not isinstance(payload, dict):
            return results
        if payload.get("object") != "whatsapp_business_account":
            return results
        for entry in payload.get("entry") or []:
            if not isinstance(entry, dict):
                continue
            for change in entry.get("changes") or []:
                if not isinstance(change, dict) or change.get("field") != "messages":
                    continue
                value = change.get("value") or {}
                if not isinstance(value, dict):
                    continue
                results.extend(self._parse_value(value))
        return results

    def _parse_value(self, value: dict) -> list[IncomingMessage]:
        out: list[IncomingMessage] = []
        metadata = value.get("metadata") or {}
        to_phone = normalize_phone(str(metadata.get("display_phone_number") or ""))
        contactos = _index_contacts(value.get("contacts") or [])

        for msg in value.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            wamid = str(msg.get("id") or "")
            mtype = str(msg.get("type") or "other")

            # Los mensajes de SISTEMA (cambio de número, identidad del cliente)
            # los genera Meta, no el cliente: si se guardan, el agente acaba
            # contestándole a un aviso de Meta con el texto vacío. Se descartan
            # con traza.
            #
            # Sus campos NO se parsean a propósito: el cambio de número ya se
            # resuelve por el BSUID, que no cambia aunque el cliente sí (ver
            # `_resolver_contacto_whatsapp` en services/conversation.py).
            if mtype == "system":
                _log_discard(
                    "meta_wa.parse.system_message_skipped",
                    "Aviso de sistema de WhatsApp (no es un mensaje del cliente): "
                    "no se contesta. Si es un cambio de número, la ficha se "
                    "actualiza sola en cuanto el cliente escriba.",
                    system_type=str((msg.get("system") or {}).get("type") or "?"),
                    has_wamid=bool(wamid),
                )
                continue
            if mtype in ("unsupported", "unknown"):
                _log_discard(
                    "meta_wa.parse.unsupported_type",
                    "WhatsApp avisa de un mensaje que su API no sabe entregar",
                    has_wamid=bool(wamid),
                    errors=str(msg.get("errors"))[:200],
                )
                continue

            from_raw = str(msg.get("from") or "")
            # El contacto se busca por teléfono y, si no lo hay, por el
            # identificador que trae el propio mensaje.
            bsuid_msg = str(msg.get("from_user_id") or "")
            contacto = (
                contactos.get(from_raw)
                or contactos.get(bsuid_msg)
                or contactos.get("")
            )
            ident = _sender_identity(msg, contacto)
            if not wamid or not (from_raw or ident.user_id):
                _log_discard(
                    "meta_wa.parse.no_identifier",
                    "Mensaje entrante descartado: sin identificador de mensaje ni de cliente",
                    msg_type=mtype,
                    has_wamid=bool(wamid),
                    has_from=bool(from_raw),
                    has_user_id=bool(ident.user_id),
                    # Solo los NOMBRES de los campos: sirve para ver si Meta
                    # cambió el esquema y no filtra ningún dato.
                    msg_keys=",".join(sorted(str(k) for k in msg))[:200],
                )
                continue

            texto, adjunto = _parse_content(msg)
            # Sin texto y sin adjunto no hay nada que enseñar: guardarlo dejaría
            # una burbuja en blanco en la bandeja y el agente contestando a un
            # mensaje vacío. Pasa, por ejemplo, cuando el cliente RETIRA una
            # reacción. Se descarta, pero con traza: un descarte mudo es lo que
            # hacía que se perdieran clientes sin que nadie se enterara.
            if texto is None and adjunto is None:
                _log_discard(
                    "meta_wa.parse.empty_content",
                    f"Mensaje de tipo «{mtype}» sin contenido que guardar",
                    msg_type=mtype,
                    msg_keys=",".join(sorted(str(k) for k in msg))[:200],
                )
                continue

            # El teléfono manda cuando está; si Meta lo omite —cliente con
            # nombre de usuario y sin contacto reciente— el contacto se
            # identifica por su BSUID, igual que en YCloud. Sin esto esos
            # mensajes se perderían enteros.
            identifier = (
                normalize_phone(from_raw)
                if from_raw
                else f"{WA_USER_PREFIX}{ident.user_id}"
            )
            tipo = mtype if mtype in ("text", "audio", *_MEDIA_TYPES) else "other"
            out.append(
                IncomingMessage(
                    provider_message_id=wamid,
                    from_phone=identifier,
                    to_phone=to_phone,
                    message_type=cast(str, tipo),  # type: ignore[arg-type]
                    text=texto,
                    audio_url=adjunto.audio_url if adjunto else None,
                    audio_mime=adjunto.audio_mime if adjunto else None,
                    media_kind=adjunto.media_kind if adjunto else None,
                    media_url=adjunto.media_url if adjunto else None,
                    media_mime=adjunto.media_mime if adjunto else None,
                    media_filename=adjunto.media_filename if adjunto else None,
                    customer_name=ident.name,
                    customer_handle=ident.handle,
                    from_user_id=ident.user_id,
                    from_parent_user_id=ident.parent_user_id,
                    raw=msg,
                )
            )
        return out

    def parse_statuses(self, payload: dict) -> list[dict]:
        """Estados de entrega (`sent`/`delivered`/`read`/`failed`).

        No se persisten —no hay modelo para ellos— pero los FALLIDOS sí se
        sacan a Monitorización desde el webhook: un mensaje que Meta rechaza es
        justo lo que hoy se pierde en silencio.

        Ref: https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/components
        """
        out: list[dict] = []
        if not isinstance(payload, dict):
            return out
        for entry in payload.get("entry") or []:
            for change in (entry or {}).get("changes") or []:
                value = (change or {}).get("value") or {}
                for st in value.get("statuses") or []:
                    if isinstance(st, dict):
                        out.append(st)
        return out

    # ------------------------------------------------------------------
    # Envío
    # ------------------------------------------------------------------

    async def _post_message(self, payload: dict, *, idempotency_key: str | None = None) -> str:
        pnid, token = await self._require_credentials()
        url = f"{GRAPH_BASE}/{pnid}/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code >= 400:
                raise RuntimeError(f"Meta {r.status_code}: {_error_detail(r)}")
            data = r.json()
        # Meta responde {"messages": [{"id": "wamid..."}], ...}
        mensajes = data.get("messages") or []
        if mensajes and isinstance(mensajes[0], dict):
            return str(mensajes[0].get("id") or "")
        return ""

    async def send_text(self, to_phone: str, body: str) -> str:
        return await self._post_message(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                **_recipient_fields(to_phone),
                "type": "text",
                # `preview_url` en falso a propósito: no queremos que un enlace
                # del agente se convierta en una tarjeta con imagen ajena.
                "text": {"preview_url": False, "body": body},
            }
        )

    async def send_media(
        self,
        to_phone: str,
        media_id: str,
        media_kind: MediaKind,
        *,
        caption: str | None = None,
        filename: str | None = None,
    ) -> str:
        """Envía un adjunto ya subido. Mismo criterio de campos que YCloud:
        el pie solo donde WhatsApp lo admite, y el nombre solo en documentos."""
        sub: dict = {"id": media_id}
        if media_kind in ("image", "video") and caption:
            sub["caption"] = caption
        if media_kind == "document":
            if filename:
                sub["filename"] = filename
            if caption:
                sub["caption"] = caption
        return await self._post_message(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                **_recipient_fields(to_phone),
                "type": media_kind,
                media_kind: sub,
            }
        )

    async def send_template(
        self,
        to_phone: str,
        template_name: str,
        language: str,
        body_variables: list[str] | None = None,
        *,
        header: dict | None = None,
        button_variables: list[dict] | None = None,
        param_format: str = "positional",
        variable_names: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Plantilla aprobada con sus componentes.

        Los componentes se montan con los MISMOS helpers que YCloud porque el
        formato es de Meta y YCloud lo reenvía tal cual: cabecera (texto o
        fichero), cuerpo y botones dinámicos.

        `idempotency_key` se ignora: la API de Meta no tiene cabecera de
        idempotencia. No es un olvido — decirlo aquí es mejor que mandar una
        cabecera que nadie mira y creer que protege de los duplicados. Lo que
        sí protege es el bloqueo por destinatario de `outbound_send`.

        Ref: https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages#template-object
        """
        components: list[dict] = []
        header_component = build_header_component(header, param_format)
        if header_component:
            components.append(header_component)
        if body_variables:
            components.append(
                {
                    "type": "body",
                    "parameters": text_parameters(
                        body_variables, param_format, variable_names
                    ),
                }
            )
        for btn in button_variables or []:
            if isinstance(btn, dict) and btn.get("parameters"):
                components.append({"type": "button", **btn})
        return await self._post_message(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                **_recipient_fields(to_phone),
                "type": "template",
                "template": {
                    "name": template_name,
                    "language": {"code": language},
                    "components": components,
                },
            }
        )

    # ------------------------------------------------------------------
    # Media
    # ------------------------------------------------------------------

    async def upload_media(self, file_bytes: bytes, mime: str, filename: str) -> str:
        """Sube el fichero y devuelve el identificador reutilizable.

        El identificador vale 30 días, así que se sube UNA vez y el mismo viaja
        en los N destinatarios de una difusión.

        Ref: https://developers.facebook.com/documentation/business-messaging/whatsapp/business-phone-numbers/media/
        """
        pnid, token = await self._require_credentials()
        url = f"{GRAPH_BASE}/{pnid}/media"
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                data={"messaging_product": "whatsapp", "type": mime},
                files={"file": (filename, file_bytes, mime)},
            )
            if r.status_code >= 400:
                raise RuntimeError(f"Meta {r.status_code}: {_error_detail(r)}")
            data = r.json()
        media_id = str(data.get("id") or "")
        if not media_id:
            logger.error(
                "meta_wa.upload.no_media_id",
                response_keys=",".join(sorted(data.keys())) if isinstance(data, dict) else "?",
            )
            raise RuntimeError("Meta no devolvió el identificador del fichero subido")
        return media_id

    async def download_audio(self, audio_url: str) -> tuple[bytes, str]:
        from app.services.agent_guardrails import MAX_AUDIO_BYTES

        return await self._download(
            audio_url, max_bytes=MAX_AUDIO_BYTES, default_mime="audio/ogg", label="Audio"
        )

    async def download_media(self, media_url: str, *, max_bytes: int) -> tuple[bytes, str]:
        return await self._download(
            media_url,
            max_bytes=max_bytes,
            default_mime="application/octet-stream",
            label="Adjunto",
        )

    async def _resolve_media_url(self, media_id: str) -> tuple[str, str]:
        """(url temporal, mime) de un identificador de media.

        La URL que devuelve Meta CADUCA A LOS CINCO MINUTOS. Por eso lo que se
        guarda en el mensaje es el identificador y no la URL: si guardáramos la
        URL, para cuando el worker fuera a descargar la nota de voz ya no
        valdría.

        Ref: https://developers.facebook.com/documentation/business-messaging/whatsapp/business-phone-numbers/media/
        """
        pnid, token = await self._require_credentials()
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{GRAPH_BASE}/{media_id}",
                params={"phone_number_id": pnid},
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code >= 400:
                raise RuntimeError(f"Meta {r.status_code}: {_error_detail(r)}")
            data = r.json()
        url = str(data.get("url") or "")
        if not url:
            raise RuntimeError("Meta no devolvió la URL de descarga del adjunto")
        return url, str(data.get("mime_type") or "")

    async def _download(
        self, referencia: str, *, max_bytes: int, default_mime: str, label: str
    ) -> tuple[bytes, str]:
        """Descarga un adjunto de Meta con todas las defensas de siempre.

        A diferencia de YCloud, aquí lo que llega guardado no es una URL sino
        `meta-media:<id>`: primero se pide la URL (que caduca en minutos) y
        luego se descarga con el token puesto, que Meta exige.
        """
        mime_declarado = ""
        if referencia.startswith(META_MEDIA_PREFIX):
            url, mime_declarado = await self._resolve_media_url(
                referencia[len(META_MEDIA_PREFIX):]
            )
        else:
            url = referencia

        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError(f"URL de adjunto inválida: {url[:80]}")
        if not _host_allowed(parsed.hostname):
            logger.error("meta_wa.download.host_blocked", host=parsed.hostname)
            raise ValueError(f"Host no permitido para descarga: {parsed.hostname}")
        if await _resolves_to_private(parsed.hostname):
            logger.error("meta_wa.download.private_ip_blocked", host=parsed.hostname)
            raise ValueError("Host resuelve a IP privada/loopback (posible SSRF)")

        _pnid, token = await self._require_credentials()
        # Descarga en STREAMING con tope: un fichero enorme no puede llevarse
        # por delante al worker, que es único (el bot entero se quedaría mudo).
        async with (
            httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client,
            client.stream(
                "GET", url, headers={"Authorization": f"Bearer {token}"}
            ) as r,
        ):
            r.raise_for_status()
            cl = r.headers.get("content-length")
            if cl and int(cl) > max_bytes:
                logger.warning("meta_wa.download.too_large_declared", bytes=cl, max=max_bytes)
                raise ValueError(
                    f"{label} de WhatsApp demasiado grande: {cl} > {max_bytes}"
                )
            mime = r.headers.get("content-type") or mime_declarado or default_mime
            chunks: list[bytes] = []
            total = 0
            async for chunk in r.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    logger.warning("meta_wa.download.too_large_stream", max=max_bytes)
                    raise ValueError(
                        f"{label} de WhatsApp excede {max_bytes} bytes durante la descarga"
                    )
                chunks.append(chunk)
            return b"".join(chunks), mime

    # ------------------------------------------------------------------
    # Plantillas
    # ------------------------------------------------------------------

    async def list_templates(self) -> list[dict]:
        """Todas las plantillas de la cuenta de negocio, con su estado.

        En Meta las plantillas cuelgan de la WABA, no del número, así que hace
        falta el identificador de la cuenta de WhatsApp Business. Se devuelven
        también las pendientes y rechazadas: la pantalla enseña el estado, y
        filtrarlas antes deja a quien mira sin saber si Meta la rechazó o
        todavía no la ha revisado.

        Recorre TODAS las páginas (cursor `paging.cursors.after`). Un fallo del
        proveedor LANZA `TemplateListError` en vez de devolver lista vacía:
        mezclar "no tienes plantillas" con "tu token no vale" es lo que hacía
        que la pantalla dijera siempre lo mismo.

        Ref: https://developers.facebook.com/docs/whatsapp/business-management-api/message-templates
        """
        token = await self._access_token()
        waba = await self._waba_id()
        if not token or not waba:
            faltan = []
            if not waba:
                faltan.append("el identificador de la cuenta de WhatsApp Business")
            if not token:
                faltan.append("el token de acceso")
            raise WhatsAppNotConfiguredError(
                "No se pueden listar las plantillas de Meta: falta "
                + " y ".join(faltan)
                + ". Complétalo en Conexiones → WhatsApp."
            )

        url = f"{GRAPH_BASE}/{waba}/message_templates"
        params: dict[str, str | int] = {
            "limit": _TEMPLATES_PAGE_SIZE,
            "fields": "name,language,status,category,components,parameter_format",
        }
        collected: list[dict] = []
        vistos: set[str] = set()
        async with httpx.AsyncClient(timeout=20.0) as client:
            for _ in range(_TEMPLATES_MAX_PAGES):
                r = await client.get(
                    url, params=params, headers={"Authorization": f"Bearer {token}"}
                )
                if r.status_code >= 400:
                    await push_runtime_log(
                        level="error",
                        event="meta_wa.templates.http_error",
                        message=f"Meta {r.status_code} listando plantillas",
                        body_preview=r.text[:200],
                    )
                    pista = (
                        " Revisa el token de acceso en Conexiones → WhatsApp."
                        if r.status_code in (400, 401, 403)
                        else ""
                    )
                    raise TemplateListError(
                        f"Meta respondió {r.status_code} al listar plantillas.{pista}"
                    )
                try:
                    data = r.json()
                except Exception as exc:
                    raise TemplateListError(
                        "Meta devolvió una respuesta que no es JSON al listar plantillas."
                    ) from exc
                items = data.get("data") if isinstance(data, dict) else None
                if not isinstance(items, list):
                    await push_runtime_log(
                        level="warn",
                        event="meta_wa.templates.unknown_shape",
                        message="No reconocí la forma de la respuesta de Meta al listar plantillas",
                        response_keys=",".join(sorted(data.keys()))
                        if isinstance(data, dict)
                        else "?",
                    )
                    raise TemplateListError(
                        "No se reconoce el formato de la respuesta de Meta al listar plantillas."
                    )
                collected.extend(t for t in items if isinstance(t, dict))

                after = (
                    ((data.get("paging") or {}).get("cursors") or {}).get("after")
                    if isinstance(data, dict)
                    else None
                )
                # Sin cursor siguiente, o cursor que no avanza: se corta. Meta
                # devuelve `after` incluso en la última página de algunas
                # cuentas, así que el freno de mano es necesario.
                if not after or after in vistos or len(items) < _TEMPLATES_PAGE_SIZE:
                    break
                vistos.add(after)
                params["after"] = after
            else:
                await push_runtime_log(
                    level="warn",
                    event="meta_wa.templates.page_cap",
                    message=f"Corté el listado de plantillas en {_TEMPLATES_MAX_PAGES} páginas",
                    collected=len(collected),
                )
        return collected


# ----------------------------------------------------------------------
# Helpers de parseo
# ----------------------------------------------------------------------


@dataclass
class _Adjunto:
    audio_url: str | None = None
    audio_mime: str | None = None
    media_kind: MediaKind | None = None
    media_url: str | None = None
    media_mime: str | None = None
    media_filename: str | None = None


@dataclass
class _Identidad:
    name: str | None = None
    handle: str | None = None
    user_id: str | None = None
    parent_user_id: str | None = None


def _index_contacts(contacts: list) -> dict[str, dict]:
    """Contactos del webhook indexados por teléfono Y por identificador.

    Por los dos porque un contacto con nombre de usuario puede llegar SIN
    `wa_id`: si solo indexáramos por teléfono, el perfil (el nombre, el
    `@usuario`) se quedaría por el camino justo en el caso que más nos
    importa.

    Se indexa además por "" para poder recuperar el único contacto cuando no
    hay por dónde casarlo: en ese caso el bloque trae uno solo.
    """
    out: dict[str, dict] = {}
    validos = [c for c in (contacts or []) if isinstance(c, dict)]
    for c in validos:
        for clave in ("wa_id", "user_id", "parent_user_id"):
            valor = str(c.get(clave) or "")
            if valor:
                out.setdefault(valor, c)
    if len(validos) == 1:
        out[""] = validos[0]
    return out


def _first_str(origen: dict | None, *claves: str) -> str | None:
    """Primer valor de texto no vacío entre varias claves candidatas."""
    if not isinstance(origen, dict):
        return None
    for k in claves:
        v = origen.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _sender_identity(msg: dict, contacto: dict | None) -> _Identidad:
    """Quién escribe: nombre, nombre de usuario e identificador ámbito-negocio.

    Es la parte crítica del proveedor. Desde abril de 2026 Meta deja de mandar
    el teléfono (`from` / `wa_id`) cuando el cliente tiene nombre de usuario y
    no ha habido contacto en los últimos 30 días ni está en la agenda; lo que
    llega siempre es el identificador ámbito-negocio (BSUID). Si exigiéramos
    teléfono, esos mensajes se perderían enteros — que es exactamente el fallo
    que se arregló en YCloud (`fromUserId` / `fromParentUserId`).

    El nombre del campo NO es el mismo en los dos sitios donde viene, y es un
    detalle que se paga caro:
      - en el MENSAJE:  `from_user_id` / `from_parent_user_id`
      - en el CONTACTO: `user_id` / `parent_user_id`
    Se leen los dos porque no todos los eventos traen el bloque de contactos.

    Ref: https://developers.facebook.com/documentation/business-messaging/whatsapp/business-scoped-user-ids/
    """
    user_id = _first_str(msg, "from_user_id") or _first_str(contacto, "user_id")
    parent = _first_str(msg, "from_parent_user_id") or _first_str(
        contacto, "parent_user_id"
    )
    perfil = (contacto or {}).get("profile") or {}
    nombre = _first_str(perfil, "name")
    usuario = _first_str(perfil, "username", "user_name")
    # El @ lo pone quien lo enseña: en YCloud viene puesto y aquí no,
    # así que se normaliza para que la bandeja los pinte igual.
    handle = None
    if usuario:
        handle = usuario if usuario.startswith("@") else f"@{usuario}"
    return _Identidad(
        name=nombre, handle=handle, user_id=user_id, parent_user_id=parent
    )


def _parse_content(msg: dict) -> tuple[str | None, _Adjunto | None]:
    """(texto, adjunto) de un mensaje entrante.

    Cubre lo que un cliente puede mandar de verdad: texto, adjuntos, el botón
    de una plantilla, la respuesta a una lista o a unos botones, una reacción y
    una ubicación. Lo que no se reconoce vuelve como (None, None) y quien
    llama lo deja anotado.
    """
    mtype = str(msg.get("type") or "")

    if mtype == "text":
        return _first_str(msg.get("text") or {}, "body"), None

    if mtype == "audio":
        audio = msg.get("audio") or {}
        media_id = _first_str(audio, "id")
        return None, _Adjunto(
            audio_url=f"{META_MEDIA_PREFIX}{media_id}" if media_id else None,
            audio_mime=_first_str(audio, "mime_type"),
        )

    if mtype in _MEDIA_TYPES:
        media = msg.get(mtype) or {}
        media_id = _first_str(media, "id")
        # El pie de foto es texto del cliente: va a `text` como el resto.
        return _first_str(media, "caption"), _Adjunto(
            media_kind=cast(MediaKind, mtype) if media_id else None,
            media_url=f"{META_MEDIA_PREFIX}{media_id}" if media_id else None,
            media_mime=_first_str(media, "mime_type"),
            media_filename=_first_str(media, "filename"),
        )

    if mtype == "button":
        # Botón de respuesta rápida de una plantilla: lo que ve el cliente es
        # el texto del botón, así que eso es lo que lee el agente.
        botón = msg.get("button") or {}
        return _first_str(botón, "text", "payload"), None

    if mtype == "interactive":
        inter = msg.get("interactive") or {}
        for clave in ("button_reply", "list_reply"):
            respuesta = inter.get(clave) or {}
            titulo = _first_str(respuesta, "title", "id")
            if titulo:
                return titulo, None
        return None, None

    if mtype == "reaction":
        # Una reacción no lleva texto propio; el emoji SÍ es información del
        # cliente ("👍" cierra muchas conversaciones). Quitarla sería perderla.
        # Sin `emoji` significa que el cliente RETIRÓ la reacción: no es un
        # mensaje vacío que guardar, es un evento que no dice nada nuevo.
        return _first_str(msg.get("reaction") or {}, "emoji"), None

    if mtype == "location":
        loc = msg.get("location") or {}
        nombre = _first_str(loc, "name", "address")
        lat, lon = loc.get("latitude"), loc.get("longitude")
        if lat is not None and lon is not None:
            return (f"{nombre} " if nombre else "") + f"({lat}, {lon})", None
        return nombre, None

    return None, None


def _error_detail(r: httpx.Response) -> str:
    """Motivo REAL que devuelve Meta, no el genérico de la librería HTTP.

    Meta contesta `{"error": {"message": ..., "code": ..., "error_subcode": ...,
    "fbtrace_id": ...}}`. El `message` es lo único que le sirve a quien está
    mirando el panel.

    Ref: https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes
    """
    try:
        err = (r.json() or {}).get("error") or {}
        partes = [str(err.get("message") or "").strip()]
        # `details` va ANIDADO dentro de `error_data`, y es donde Meta pone lo
        # concreto ("el parámetro X no vale"); el `message` a secas suele ser
        # genérico. Si `error_data` no es un diccionario, se ignora sin más.
        datos = err.get("error_data")
        if isinstance(datos, dict):
            detalle = str(datos.get("details") or "").strip()
            if detalle:
                partes.append(detalle)
        code = err.get("code")
        if code is not None:
            partes.append(f"(código {code})")
        texto = " ".join(p for p in partes if p)
        if texto:
            return texto[:500]
    except Exception:  # noqa: BLE001 — si el cuerpo no es JSON, vale el texto
        pass
    return r.text[:500]
