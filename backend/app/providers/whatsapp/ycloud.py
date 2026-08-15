"""Implementación YCloud del WhatsAppProvider.

Las credenciales (api_key, webhook_secret, phone_number) viven cifradas en la
BD (tabla `credentials`) y se leen vía `app.services.credentials.get_credential`
en cada llamada. Cambiar una cred desde `/admin/credentials` aplica sin reiniciar.

YCloud envía webhooks con un payload tipo:
{
  "id": "...",
  "type": "whatsapp.inbound_message.received",
  "whatsappInboundMessage": {
    "id": "wamid....",
    "from": "+34666...",
    "to": "+34900...",
    "fromUserId": "ES.1349120865...",
    "fromParentUserId": "ES.ENT.118157...",
    "customerProfile": { "name": "Joe", "username": "@JoeJoe" },
    "type": "text" | "audio" | "image" | ...,
    "text": { "body": "..." },
    "audio": { "id": "...", "mime_type": "audio/ogg", "link": "https://..." }
  }
}

OJO con `from`: desde abril de 2026, cuando el cliente tiene NOMBRE DE USUARIO de
WhatsApp, Meta OMITE `from`/`wa_id` y solo manda el identificador ámbito-negocio
(BSUID, `fromUserId`). El teléfono únicamente viaja si hubo mensaje o llamada en
los últimos 30 días o si el cliente está en la agenda. Por eso el identificador
del contacto es "el teléfono si lo hay, y si no `wa:<bsuid>`": si exigiéramos
teléfono, esos mensajes se perderían enteros.

Docs: https://www.ycloud.com/docs
      https://docs.ycloud.com/reference/whatsapp-inbound-message-webhook-examples
      https://developers.facebook.com/docs/whatsapp/business-scoped-user-ids/
"""
import asyncio
import hashlib
import hmac
import ipaddress
import socket
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
    text_parameters,
)
from app.providers.whatsapp.base import (
    normalize_phone as _normalize_phone,
)
from app.services.credentials import CredentialUnavailableError, get_credential
from app.services.runtime_logs import push_runtime_log

logger = get_logger(__name__)

# Los tres errores y `_normalize_phone` vivían aquí y medio backend los importa
# de este módulo (`from app.providers.whatsapp.ycloud import ...`). Al añadirse
# el proveedor de Meta han pasado a `base.py` —son comunes a los dos—, pero se
# re-exportan desde aquí para no tener que tocar una docena de imports.
__all__ = [
    "TemplateListError",
    "WhatsAppCredentialsUnreadableError",
    "WhatsAppNotConfiguredError",
    "YCloudProvider",
    "_normalize_phone",
    "get_whatsapp_provider",
]


# Tope de páginas al listar plantillas: 20 × 100 = 2000 plantillas. Es un
# freno de mano contra un cursor que no avanza, no un límite de negocio.
_TEMPLATES_MAX_PAGES = 20
_TEMPLATES_PAGE_SIZE = 100

# Hosts permitidos para descargar adjuntos de WhatsApp. YCloud aloja los media
# bajo *.ycloud.com / api.ycloud.com. Si en algún momento usan CDN, añadir aquí.
_ALLOWED_DOWNLOAD_SUFFIXES = (
    ".ycloud.com",
    "ycloud.com",
)


# `parse_webhook` es SÍNCRONO (lo fija la interfaz WhatsAppProvider) pero
# `push_runtime_log` es una corrutina: llamarla a pelo desde aquí no ejecuta
# nada. La programamos en el loop que ya está corriendo (el del request del
# webhook) y guardamos la referencia mientras vive, porque una tarea sin
# referencias fuertes se la puede llevar el recolector a medio ejecutar.
_pending_log_tasks: set[asyncio.Task] = set()


def _log_discard(event: str, message: str, **fields: object) -> None:
    """Deja traza de un mensaje entrante que descartamos.

    Antes el `continue` era MUDO: si un mensaje se caía aquí no quedaba ni una
    línea en ningún sitio, el webhook devolvía 200 y YCloud lo daba por
    entregado. Se perdían clientes reales sin saberlo. Nunca más un descarte sin
    rastro. Solo metadatos (banderas, tipos y NOMBRES de campo), nunca el
    contenido ni el teléfono en claro.
    """
    logger.warning(event, **fields)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # fuera de un event loop (p. ej. tests síncronos): basta el logger
    task = loop.create_task(
        push_runtime_log(level="warn", event=event, message=message, **fields)
    )
    _pending_log_tasks.add(task)
    task.add_done_callback(_pending_log_tasks.discard)


def _recipient_fields(destination: str) -> dict[str, str]:
    """Campo de destinatario para la API de YCloud: `to` O `recipient`, nunca los dos.

    La API exige EXACTAMENTE uno. Si van los dos, `to` tiene precedencia y
    `recipient` se ignora, así que mandar ambos con un `to` inventado enviaría el
    mensaje a un número que no existe. El BSUID viaja literal, sin truncar (hasta
    128 caracteres más el prefijo de país, `ES.xxxx`); el parent lleva otro
    formato, `ES.ENT.xxxx`, y `recipient` acepta los dos.

    Ref: https://docs.ycloud.com/reference/whatsapp_message-send
    """
    if destination.startswith(WA_USER_PREFIX):
        return {"recipient": destination[len(WA_USER_PREFIX):]}
    return {"to": _normalize_phone(destination)}


def _host_allowed(host: str) -> bool:
    h = host.lower()
    return any(h == s or h.endswith("." + s.lstrip(".")) for s in _ALLOWED_DOWNLOAD_SUFFIXES)


async def _resolves_to_private(host: str) -> bool:
    """`getaddrinfo` es síncrono; lo movemos al executor para no bloquear el
    event loop con resoluciones DNS lentas."""
    loop = asyncio.get_event_loop()
    try:
        infos = await loop.run_in_executor(None, socket.getaddrinfo, host, None)
    except socket.gaierror:
        return True  # si no resuelve, lo tratamos como inseguro
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
    return False


class YCloudProvider(WhatsAppProvider):
    def __init__(self) -> None:
        self.base_url = settings.YCLOUD_BASE_URL

    async def _strict_credential(self, key: str) -> str | None:
        """Credencial en modo ESTRICTO: None si no está, LANZA si no se abre.

        Distinguir los dos casos importa: "no configurada" se arregla
        escribiendo la credencial, y "guardada pero no se descifra" (típico tras
        cambiar `ENCRYPTION_KEY`) se arregla restaurando la clave. Confundirlos
        manda al operador a reintroducir credenciales que en realidad están bien.
        """
        try:
            return await get_credential(key, strict=True)
        except CredentialUnavailableError as exc:
            raise WhatsAppCredentialsUnreadableError(
                f"La credencial '{key}' está guardada pero no se puede descifrar "
                "(¿cambió ENCRYPTION_KEY?). No se ha enviado nada."
            ) from exc

    async def _api_key(self) -> str | None:
        return await self._strict_credential("ycloud_api_key")

    async def _webhook_secret(self) -> str | None:
        # NO estricto: verificar la firma tiene su propio camino de error (log +
        # False) y no queremos que una credencial ilegible tumbe el webhook.
        return await get_credential("ycloud_webhook_secret")

    async def _from_phone(self) -> str:
        return (await self._strict_credential("ycloud_phone_number")) or ""

    async def _require_credentials(self) -> tuple[str, str]:
        """Devuelve (api_key, from_phone) o LANZA. Nunca devuelve vacíos."""
        api_key = await self._api_key()
        from_phone = await self._from_phone()
        missing = []
        if not api_key:
            missing.append("ycloud_api_key")
        if not from_phone:
            missing.append("ycloud_phone_number")
        if missing:
            raise WhatsAppNotConfiguredError(
                "WhatsApp no está configurado: falta "
                + " y ".join(missing)
                + " en /admin/credentials. No se ha enviado nada."
            )
        return cast(str, api_key), from_phone

    async def check_credentials(self) -> None:
        """Comprueba que se puede enviar. Lanza si no. No llama a la API.

        La usa `create_outbound_job` ANTES de aceptar una campaña: es preferible
        un 400 al crear que 300 errores uno a uno (o, peor, 300 falsos éxitos).
        """
        await self._require_credentials()

    async def verify_webhook_signature(self, headers: dict[str, str], body: bytes) -> bool:
        """Verifica la firma del webhook de YCloud.

        YCloud envia el header `YCloud-Signature` (sin prefijo X-) con
        formato `t=<unix_timestamp>,s=<hex_hmac>`. La firma se calcula como
        HMAC-SHA256 sobre `{timestamp}.{body}`, no sobre `body` solo.

        Ref: https://docs.ycloud.com/reference/webhook-integration-guide
        """
        secret = await self._webhook_secret()
        if not secret:
            logger.error("ycloud.webhook.no_secret_configured")
            await push_runtime_log(
                level="error",
                event="webhook.signature.no_secret",
                message="No hay 'ycloud_webhook_secret' configurado en /admin/credentials",
            )
            return False
        sig_header = (
            headers.get("ycloud-signature")
            or headers.get("YCloud-Signature")
            or headers.get("x-ycloud-signature")
            or headers.get("X-YCloud-Signature")
        )
        if not sig_header:
            logger.error("ycloud.webhook.no_signature_header")
            await push_runtime_log(
                level="warn",
                event="webhook.signature.no_header",
                message="Webhook llego sin header YCloud-Signature (igual no es de YCloud?)",
                headers_keys=",".join(list(headers.keys())[:8]),
            )
            return False
        parts: dict[str, str] = {}
        for el in sig_header.split(","):
            k, _, v = el.strip().partition("=")
            if k:
                parts[k] = v
        timestamp = parts.get("t")
        signature = parts.get("s")
        if not timestamp or not signature:
            logger.error("ycloud.webhook.malformed_signature", header_preview=sig_header[:60])
            await push_runtime_log(
                level="warn",
                event="webhook.signature.malformed",
                message=f"Header con formato raro: {sig_header[:80]}",
            )
            return False
        signed_payload = f"{timestamp}.".encode() + body
        expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
        ok = hmac.compare_digest(expected, signature)
        if not ok:
            # Detalle util sin revelar NADA del secret: solo su longitud (los
            # primeros chars de los HMAC no son sensibles). Antes se logueaban
            # 8 chars del secret, suficiente para acotar un brute-force.
            await push_runtime_log(
                level="error",
                event="webhook.signature.mismatch",
                message="HMAC no coincide. Probablemente el secret de /admin/credentials no es el mismo que el de YCloud.",
                secret_len=len(secret),
                expected_hint=expected[:8] + "...",
                received_hint=signature[:8] + "...",
                timestamp_recv=timestamp,
                body_len=len(body),
            )
        return ok

    def parse_webhook(self, payload: dict) -> list[IncomingMessage]:
        results: list[IncomingMessage] = []
        events = payload if isinstance(payload, list) else [payload]
        for ev in events:
            if not isinstance(ev, dict):
                continue
            if ev.get("type") != "whatsapp.inbound_message.received":
                continue
            msg = ev.get("whatsappInboundMessage") or {}
            wamid = msg.get("id")
            from_phone = msg.get("from") or ""
            to_phone = msg.get("to") or ""
            mtype = msg.get("type") or "other"
            # Los mensajes de SISTEMA (cambio de número, cliente identificado…)
            # no los escribe el cliente: los genera Meta. Traen `id` y a veces
            # `from`, así que colaban el filtro de arriba y se guardaban como si
            # fueran un mensaje más — con el texto vacío y el agente
            # contestándole a un aviso de Meta. Se descartan con traza.
            #
            # NO se parsean sus campos a propósito. El caso que importa —el
            # cambio de número— ya se resuelve solo por otro lado: el BSUID no
            # cambia cuando el cliente cambia de teléfono, así que en cuanto
            # escribe desde el número nuevo su ficha se encuentra igual y se
            # actualiza (ver `_resolver_contacto_whatsapp`). Leer aquí unos
            # nombres de campo que no hemos podido confirmar en la documentación
            # solo serviría para escribir el teléfono equivocado en la ficha de
            # alguien.
            if mtype == "system":
                system = msg.get("system") or {}
                _log_discard(
                    "ycloud.parse.system_message_skipped",
                    "Aviso de sistema de WhatsApp (no es un mensaje del cliente): "
                    "no se contesta. Si es un cambio de número, la ficha se "
                    "actualiza sola en cuanto el cliente escriba.",
                    system_type=system.get("type") or "?",
                    has_wamid=bool(wamid),
                )
                continue
            # Identificador ámbito-negocio (BSUID). Llega SIEMPRE; el teléfono no.
            from_user_id = (msg.get("fromUserId") or "").strip()
            from_parent_user_id = (msg.get("fromParentUserId") or "").strip() or None
            text = None
            audio_url = None
            audio_mime = None
            media_kind = None
            media_url = None
            media_mime = None
            media_filename = None
            if mtype == "text":
                text = (msg.get("text") or {}).get("body")
            elif mtype == "audio":
                audio = msg.get("audio") or {}
                audio_url = audio.get("link") or audio.get("url")
                audio_mime = audio.get("mime_type")
            elif mtype in ("image", "video", "document", "sticker"):
                # Mismo formato que el audio: {link|url, mime_type, caption}.
                # Sin esto el mensaje se guardaba vacío (burbuja en blanco en la
                # bandeja) y el agente no se enteraba de que había un adjunto.
                media = msg.get(mtype) or {}
                media_url = media.get("link") or media.get("url")
                media_mime = media.get("mime_type")
                media_filename = media.get("filename")
                # El pie de foto es texto del cliente: va a `text` como el resto.
                text = media.get("caption") or None
                if media_url:
                    media_kind = mtype
            if not wamid or not (from_phone or from_user_id):
                _log_discard(
                    "ycloud.parse.no_identifier",
                    "Mensaje entrante descartado: sin wamid o sin teléfono NI BSUID",
                    msg_type=mtype,
                    has_wamid=bool(wamid),
                    has_from=bool(from_phone),
                    has_user_id=bool(from_user_id),
                    # Solo los NOMBRES de los campos que trajo el payload: sirve
                    # para ver si YCloud cambió el esquema, y no filtra datos.
                    msg_keys=",".join(sorted(str(k) for k in msg))[:200],
                )
                continue
            # El teléfono manda cuando está; si Meta lo omite (cliente con
            # nombre de usuario) el contacto se identifica por su BSUID.
            identifier = (
                _normalize_phone(from_phone)
                if from_phone
                else f"{WA_USER_PREFIX}{from_user_id}"
            )
            # WhatsApp pasa el nombre del contacto en `customerProfile.name` y,
            # desde 2026, el nombre de usuario en `.username` (ya viene con la @).
            # El handle reutiliza `customer_handle` (→ Contact.social_handle),
            # el mismo campo que se creó para el @usuario de Instagram.
            customer_profile = msg.get("customerProfile") or {}
            customer_name = (customer_profile.get("name") or "").strip() or None
            customer_handle = (customer_profile.get("username") or "").strip() or None
            results.append(
                IncomingMessage(
                    provider_message_id=wamid,
                    from_phone=identifier,
                    to_phone=_normalize_phone(to_phone),
                    message_type=cast(str, mtype if mtype in {"text", "audio", "image", "video", "document", "sticker"} else "other"),  # type: ignore[arg-type]
                    text=text,
                    audio_url=audio_url,
                    audio_mime=audio_mime,
                    media_kind=media_kind,
                    media_url=media_url,
                    media_mime=media_mime,
                    media_filename=media_filename,
                    customer_name=customer_name,
                    customer_handle=customer_handle,
                    from_user_id=from_user_id or None,
                    from_parent_user_id=from_parent_user_id,
                    raw=msg,
                )
            )
        return results

    def parse_statuses(self, payload: dict | list) -> list[dict]:
        """Estados de entrega de los mensajes que hemos enviado nosotros.

        YCloud los manda en un evento aparte, `whatsapp.message.updated`, con
        el mensaje en `whatsappMessage`. Este webhook llegaba y se tiraba
        entero: `parse_webhook` solo mira los `whatsapp.inbound_message.received`
        y todo lo demás se ignora sin dejar rastro. O sea que un mensaje que
        WhatsApp NO había podido entregar —número inexistente, cliente que
        bloqueó a la empresa, ventana cerrada— seguía apareciendo como enviado
        en la bandeja.

        Se devuelve el `id` de YCloud y el `wamid` de Meta porque el envío
        guarda uno u otro según lo que traiga la respuesta (`send_text`
        devuelve `id or wamid`), y el estado tiene que poder encontrar el
        mensaje con cualquiera de los dos.

        Ref: docs.ycloud.com/reference/whatsapp-message-updated-webhook-examples
        """
        out: list[dict] = []
        events = payload if isinstance(payload, list) else [payload]
        for ev in events:
            if not isinstance(ev, dict):
                continue
            if ev.get("type") != "whatsapp.message.updated":
                continue
            msg = ev.get("whatsappMessage") or {}
            if not isinstance(msg, dict):
                continue
            estado = (msg.get("status") or "").strip()
            if not estado:
                continue
            out.append(
                {
                    "id": str(msg.get("id") or ""),
                    "wamid": str(msg.get("wamid") or ""),
                    "status": estado,
                    "error_code": str(msg.get("errorCode") or ""),
                    "error_message": str(msg.get("errorMessage") or ""),
                }
            )
        return out

    async def list_templates(self) -> list[dict]:
        """Lista TODAS las plantillas WhatsApp registradas en YCloud.

        Devolvemos también las pending/rejected — la UI muestra el `status`
        de cada una. Si filtramos antes, el administrador no ve qué pasa cuando una
        plantilla no aparece y no sabe si Meta la rechazó o aún no la
        revisó. Es UX mejor mostrar todas + el estado.

        RECORRE TODAS LAS PÁGINAS. Antes pedía 100 y se quedaba ahí: con más de
        100 plantillas la que buscabas sencillamente no salía y nadie avisaba.
        Soporta las dos formas de paginar que usa YCloud según endpoint y
        versión: cursor (`nextPageToken`/`next_cursor`) y número de página
        (`page`/`total`).

        Un fallo del proveedor (401, 500, JSON roto) LANZA `TemplateListError`.
        Devolver [] mezclaba "no tienes plantillas" con "tu clave no vale".

        Acepta payloads distintos según versión de YCloud:
          - {data: [...]}, {items: [...]}, {whatsappTemplates: [...]}, [...]
        """
        api_key = await self._api_key()
        from app.services.runtime_logs import push_runtime_log
        if not api_key:
            await push_runtime_log(
                level="warn",
                event="ycloud.templates.no_api_key",
                message="ycloud_api_key no configurada en /admin/credentials",
            )
            raise WhatsAppNotConfiguredError(
                "Falta ycloud_api_key en /admin/credentials: no se pueden listar plantillas."
            )
        url = f"{self.base_url}/whatsapp/templates"
        # YCloud pagina con limit=10 POR DEFECTO y, según la cuenta, no lista
        # nada sin filtrar por WABA. Subimos el límite y filtramos por el WABA
        # del número configurado (lo resolvemos vía /whatsapp/phoneNumbers).
        base_params: dict[str, str | int] = {"limit": _TEMPLATES_PAGE_SIZE}
        collected: list[dict] = []
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                from_phone = await self._from_phone()
                if from_phone:
                    rp = await client.get(
                        f"{self.base_url}/whatsapp/phoneNumbers",
                        headers={"X-API-Key": api_key},
                        params={"limit": 100},
                    )
                    if rp.status_code < 400:
                        pdata = rp.json()
                        plist = pdata.get("items") if isinstance(pdata, dict) else pdata
                        digits = "".join(ch for ch in from_phone if ch.isdigit())
                        for pn in plist or []:
                            num = "".join(ch for ch in str(pn.get("phoneNumber", "")) if ch.isdigit())
                            if num and (num == digits or num.endswith(digits) or digits.endswith(num)):
                                if pn.get("wabaId"):
                                    base_params["filter.wabaId"] = pn["wabaId"]
                                break
            except Exception:
                pass  # el filtro por WABA es best-effort; sin él se lista igual

            cursor: str | None = None
            cursor_param = "pageToken"
            page_number = 1
            seen_cursors: set[str] = set()
            for _ in range(_TEMPLATES_MAX_PAGES):
                params = dict(base_params)
                if cursor:
                    params[cursor_param] = cursor
                elif page_number > 1:
                    params["page"] = page_number
                r = await client.get(url, headers={"X-API-Key": api_key}, params=params)
                if r.status_code == 404:
                    await push_runtime_log(
                        level="warn",
                        event="ycloud.templates.404",
                        message="YCloud devolvió 404 listando plantillas (endpoint cambiado?)",
                    )
                    raise TemplateListError(
                        "YCloud devolvió 404 al listar plantillas (¿endpoint cambiado?)."
                    )
                if r.status_code >= 400:
                    await push_runtime_log(
                        level="error",
                        event="ycloud.templates.http_error",
                        message=f"YCloud {r.status_code} listando plantillas",
                        body_preview=r.text[:200],
                    )
                    hint = (
                        " Revisa `ycloud_api_key` en /admin/credentials."
                        if r.status_code in (401, 403)
                        else ""
                    )
                    raise TemplateListError(
                        f"YCloud respondió {r.status_code} al listar plantillas.{hint}"
                    )
                try:
                    data = r.json()
                except Exception as exc:
                    await push_runtime_log(
                        level="error",
                        event="ycloud.templates.bad_json",
                        message="YCloud devolvió respuesta no-JSON",
                    )
                    raise TemplateListError(
                        "YCloud devolvió una respuesta que no es JSON al listar plantillas."
                    ) from exc

                items = _templates_items(data)
                if items is None:
                    await push_runtime_log(
                        level="warn",
                        event="ycloud.templates.unknown_shape",
                        message="No reconocí la forma de la respuesta YCloud /whatsapp/templates",
                        response_keys=",".join(data.keys()) if isinstance(data, dict) else "list",
                        first_chars=str(data)[:200],
                    )
                    raise TemplateListError(
                        "No se reconoce el formato de la respuesta de YCloud al listar plantillas."
                    )
                collected.extend(t for t in items if isinstance(t, dict))

                next_cursor, next_param = _templates_next_cursor(data)
                if next_cursor:
                    if next_cursor in seen_cursors:
                        break  # el cursor no avanza: cortamos en vez de girar sin fin
                    seen_cursors.add(next_cursor)
                    cursor, cursor_param = next_cursor, next_param
                    continue
                # Sin cursor: paginación por número de página. Solo seguimos si
                # la página venía LLENA y el total declarado (si lo hay) da más.
                if len(items) < _TEMPLATES_PAGE_SIZE:
                    break
                total = data.get("total") if isinstance(data, dict) else None
                if isinstance(total, int) and len(collected) >= total:
                    break
                page_number += 1
            else:
                await push_runtime_log(
                    level="warn",
                    event="ycloud.templates.page_cap",
                    message=f"Corté el listado de plantillas en {_TEMPLATES_MAX_PAGES} páginas",
                    collected=len(collected),
                )

        return collected

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
        """Envía una plantilla pre-aprobada con sus variables.

        Componentes soportados:
          - `body`: los `body_variables`, en orden.
          - `header`: texto con variables, o media (imagen/vídeo/documento).
            `header` es `{"format": "image"|"video"|"document"|"text",
            "media_id": "...", "link": "...", "filename": "...",
            "variables": ["…"]}`. Antes solo se montaba el body, así que
            cualquier plantilla con cabecera fallaba en TODOS los destinatarios
            con un error del proveedor sin traducir.
          - `buttons`: parámetros de botones dinámicos, ya en formato
            componente (`{"sub_type", "index", "parameters"}`).

        `param_format="named"` monta `parameter_name` en cada parámetro: es lo
        que genera hoy el asistente de plantillas de Meta (`{{nombre}}`), y sin
        esto se enviaban cero parámetros y Meta rechazaba el envío entero.

        `idempotency_key`: viaja como cabecera `Idempotency-Key`. Si el worker
        muere entre la llamada y el commit, el reintento manda la MISMA clave y
        el proveedor puede descartar el duplicado en vez de mandarlo dos veces.
        """
        api_key, from_phone = await self._require_credentials()
        components: list[dict] = []
        header_component = _build_header_component(header, param_format)
        if header_component:
            components.append(header_component)
        if body_variables:
            components.append(
                {
                    "type": "body",
                    "parameters": _text_parameters(
                        body_variables, param_format, variable_names
                    ),
                }
            )
        for btn in button_variables or []:
            if isinstance(btn, dict) and btn.get("parameters"):
                components.append({"type": "button", **btn})
        payload = {
            "from": from_phone,
            # `to` (E.164) o `recipient` (BSUID), según el identificador guardado.
            **_recipient_fields(to_phone),
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
                "components": components,
            },
        }
        url = f"{self.base_url}/whatsapp/messages/sendDirectly"
        headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = str(idempotency_key)
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code >= 400:
                # Exponemos el motivo real de YCloud (cuerpo) en vez del genérico httpx.
                detail: object = r.text
                try:
                    j = r.json()
                    detail = j.get("message") or j.get("error") or j.get("errors") or r.text
                except Exception:
                    pass
                logger.error("ycloud.send_template.error", status=r.status_code, detail=str(detail)[:500])
                raise RuntimeError(f"YCloud {r.status_code}: {detail}")
            data = r.json()
        return str(data.get("id") or data.get("wamid") or "")

    async def send_text(self, to_phone: str, body: str) -> str:
        api_key, from_phone = await self._require_credentials()
        url = f"{self.base_url}/whatsapp/messages/sendDirectly"
        payload = {
            "from": from_phone,
            # `to` (E.164) o `recipient` (BSUID), según el identificador guardado.
            **_recipient_fields(to_phone),
            "type": "text",
            "text": {"body": body},
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                url,
                json=payload,
                headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            )
            r.raise_for_status()
            data = r.json()
        return str(data.get("id") or data.get("wamid") or "")

    async def upload_media(self, file_bytes: bytes, mime: str, filename: str) -> str:
        """Sube el media a YCloud y devuelve el media_id reutilizable.

        YCloud (igual que Meta Cloud) acepta multipart en
        `POST /whatsapp/media/upload` con campos `file` y `type`.
        Devuelve `{ "id": "...", "url": "..." }`.
        """
        api_key = await self._api_key()
        if not api_key:
            logger.error("ycloud.upload.no_api_key")
            raise RuntimeError("ycloud_api_key no configurada")
        url = f"{self.base_url}/whatsapp/media/upload"
        files = {"file": (filename, file_bytes, mime)}
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                url,
                files=files,
                headers={"X-API-Key": api_key},
            )
            r.raise_for_status()
            data = r.json()
        media_id = str(data.get("id") or data.get("mediaId") or "")
        if not media_id:
            logger.error("ycloud.upload.no_media_id", response_keys=",".join(data.keys()))
            raise RuntimeError("YCloud no devolvió media_id")
        return media_id

    async def send_media(
        self,
        to_phone: str,
        media_id: str,
        media_kind: MediaKind,
        *,
        caption: str | None = None,
        filename: str | None = None,
    ) -> str:
        """Envía un mensaje multimedia con el media_id previamente subido.

        Formato del payload por tipo:
          - image:    { "type": "image",    "image":    {"id": ..., "caption"?} }
          - audio:    { "type": "audio",    "audio":    {"id": ...} }
          - video:    { "type": "video",    "video":    {"id": ..., "caption"?} }
          - document: { "type": "document", "document": {"id": ..., "filename"?, "caption"?} }
          - sticker:  { "type": "sticker",  "sticker":  {"id": ...} }
        """
        api_key, from_phone = await self._require_credentials()

        # Construye el sub-payload según tipo, solo añadiendo campos cuando
        # WhatsApp los acepta. caption/filename son opcionales.
        sub: dict = {"id": media_id}
        if media_kind in ("image", "video") and caption:
            sub["caption"] = caption
        if media_kind == "document":
            if filename:
                sub["filename"] = filename
            if caption:
                sub["caption"] = caption
        payload = {
            "from": from_phone,
            # `to` (E.164) o `recipient` (BSUID), según el identificador guardado.
            **_recipient_fields(to_phone),
            "type": media_kind,
            media_kind: sub,
        }
        url = f"{self.base_url}/whatsapp/messages/sendDirectly"
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                url,
                json=payload,
                headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            )
            r.raise_for_status()
            data = r.json()
        return str(data.get("id") or data.get("wamid") or "")

    async def download_audio(self, audio_url: str) -> tuple[bytes, str]:
        from app.services.agent_guardrails import MAX_AUDIO_BYTES

        return await self._download_file(
            audio_url, max_bytes=MAX_AUDIO_BYTES, default_mime="audio/ogg", label="Audio"
        )

    async def download_media(self, media_url: str, *, max_bytes: int) -> tuple[bytes, str]:
        """Descarga un adjunto que no es nota de voz (imagen, vídeo, documento).

        Mismas defensas que el audio: HTTPS, lista blanca de hosts, anti-SSRF y
        tope de bytes en streaming.
        """
        return await self._download_file(
            media_url,
            max_bytes=max_bytes,
            default_mime="application/octet-stream",
            label="Adjunto",
        )

    async def _download_file(
        self, audio_url: str, *, max_bytes: int, default_mime: str, label: str
    ) -> tuple[bytes, str]:
        parsed = urlparse(audio_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"URL de audio inválida: {audio_url}")
        if parsed.scheme != "https":
            raise ValueError("Solo se permite HTTPS para descargar audios")
        if not _host_allowed(parsed.hostname):
            logger.error("audio.download.host_blocked", host=parsed.hostname)
            raise ValueError(f"Host no permitido para descarga: {parsed.hostname}")
        if await _resolves_to_private(parsed.hostname):
            logger.error("audio.download.private_ip_blocked", host=parsed.hostname)
            raise ValueError("Host resuelve a IP privada/loopback (posible SSRF)")
        api_key = await self._api_key()
        # Descarga en STREAMING con tope de tamaño. Antes era `r.content`: el
        # fichero entero en la RAM del worker ANTES de mirar cuánto ocupaba (el
        # tope se comprobaba después, ya en audio_processor, cuando los bytes
        # estaban dentro). Un media enorme —o un servidor que responde sin
        # fin— se llevaba por delante al worker, que es único: el bot entero
        # mudo. Mismo patrón que el provider de Instagram (instagram/meta.py).
        headers = {"X-API-Key": api_key} if api_key else None
        async with (
            httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client,
            client.stream("GET", audio_url, headers=headers) as r,
        ):
            r.raise_for_status()
            # 1) Corta ya si el servidor DECLARA un tamaño excesivo: no se
            #    llega a leer ni un byte del cuerpo.
            cl = r.headers.get("content-length")
            if cl and int(cl) > max_bytes:
                logger.warning(
                    "audio.download.too_large_declared",
                    bytes=cl,
                    max=max_bytes,
                )
                raise ValueError(
                    f"{label} de WhatsApp demasiado grande: {cl} > {max_bytes}"
                )
            mime = r.headers.get("content-type", default_mime)
            # 2) Y corta DURANTE la descarga aunque no venga Content-Length
            #    (Transfer-Encoding: chunked, o cabecera mentirosa).
            chunks: list[bytes] = []
            total = 0
            async for chunk in r.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    logger.warning(
                        "audio.download.too_large_stream", max=max_bytes
                    )
                    raise ValueError(
                        f"{label} de WhatsApp excede {max_bytes} bytes "
                        "durante la descarga"
                    )
                chunks.append(chunk)
            return b"".join(chunks), mime


# Los tres helpers de plantilla viven ahora en `base.py`: el formato de los
# componentes es el de Meta y YCloud lo reenvía tal cual, así que lo comparten
# los dos proveedores. Se re-exportan con el nombre de antes por si algo los
# importaba de aquí.
_text_parameters = text_parameters
_build_header_component = build_header_component


def _templates_items(data: object) -> list | None:
    """Saca la lista de plantillas de la respuesta, sea cual sea su envoltorio.

    OJO: no usar `or` encadenado — una lista VACÍA es respuesta válida (cero
    plantillas) y el `or` la confundía con "clave ausente".
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "items", "whatsappTemplates", "templates"):
            v = data.get(key)
            if isinstance(v, list):
                return v
    return None


def _templates_next_cursor(data: object) -> tuple[str | None, str]:
    """(cursor de la página siguiente, nombre del parámetro con el que se pide).

    YCloud no usa el mismo nombre en todos los endpoints/versiones, así que
    aceptamos los habituales y devolvemos con qué parámetro hay que pedirlo.
    """
    if not isinstance(data, dict):
        return None, "pageToken"
    for key, param in (
        ("nextPageToken", "pageToken"),
        ("next_page_token", "pageToken"),
        ("nextCursor", "cursor"),
        ("next_cursor", "cursor"),
        ("startingAfter", "startingAfter"),
    ):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip(), param
    return None, "pageToken"


def get_whatsapp_provider() -> WhatsAppProvider:
    """Re-export del selector, que es quien decide entre YCloud y Meta.

    Esta función vivía aquí y elegía por variable de entorno; la elección es
    ahora por canal y desde el panel, así que la decisión la toma
    `selector.py`. Se mantiene el nombre y el módulo porque
    `app/services/media.py` importa la función DE AQUÍ.
    """
    from app.providers.whatsapp.selector import get_whatsapp_provider as _get

    return _get()
