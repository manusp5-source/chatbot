"""Cliente REST Gmail (F5a lectura + F5c borradores del agente).

Wrapper minimalista sobre la Gmail v1 REST API. No usamos
`google-api-python-client` para no añadir dependencia pesada — bastan httpx +
el access_token que da `google_oauth.get_access_token("google_gmail")` (mismo
patrón que `services/google_calendar.py`).

En F5a SOLO leíamos: perfil, historial de cambios, listado de mensajes (para el
bootstrap) y el contenido de cada mensaje. F5c añade la capacidad de BORRADOR:
el agente NUNCA envía un correo automáticamente, genera un borrador REAL en
Gmail (drafts.*) para revisión humana. El envío manual del borrador lo dispara
la operadora desde el panel. (El envío libre fuera del flujo de borradores es
F5b y va aparte.)

Métodos de lectura (F5a):
  - get_profile()                  → users.getProfile (emailAddress + historyId)
  - list_history(start_history_id) → users.history.list (mensajes nuevos)
  - list_messages(query)           → users.messages.list?q=  (bootstrap)
  - get_message(message_id)        → users.messages.get?format=full (parseado)

Métodos de borrador (F5c — requieren scope gmail.modify, ya concedido):
  - create_draft(...)              → users.drafts.create (borrador en el hilo)
  - update_draft(draft_id, ...)    → users.drafts.update (re-edita el raw)
  - send_draft(draft_id)           → users.drafts.send (envía el borrador)
  - delete_draft(draft_id)         → users.drafts.delete (limpieza best-effort)

`parse_message_to_incoming(raw)` convierte el dict ya parseado por `get_message`
en un `IncomingMessage` (la misma dataclass que usa WhatsApp/Instagram).

Adjuntos: en F5a están DIFERIDOS. Si un correo trae adjuntos, añadimos un
marcador "[adjunto: filename]" al cuerpo y registramos sus nombres en la
metadata, pero NO descargamos el binario.

Docs: https://developers.google.com/gmail/api/reference/rest
"""
from __future__ import annotations

import base64
import re
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any

import httpx

from app.core.logging import get_logger
from app.core.redis import get_redis

# IncomingMessage es stdlib-puro (dataclass) y se importa directo del módulo
# base, igual que hace el provider de Instagram, para reutilizar la
# normalización de mensaje entrante.
from app.providers.whatsapp.base import IncomingMessage

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1"
GMAIL_PROVIDER = "google_gmail"

logger = get_logger(__name__)


def _ensure_re_subject(subject: str | None) -> str:
    """Prefija 'Re: ' al asunto si no lo tiene ya (case-insensitive). Si no hay
    asunto, devuelve un 'Re:' a secas (Gmail lo acepta)."""
    subj = (subject or "").strip()
    if not subj:
        return "Re:"
    if subj.lower().startswith("re:"):
        return subj
    return f"Re: {subj}"


def build_raw_message(
    to_addr: str,
    subject: str,
    body_text: str,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> str:
    """Construye un mensaje RFC2822 (text/plain, UTF-8) y lo devuelve en
    base64url SIN padding (formato que espera Gmail en `raw`).

    Cabeceras: To, Subject (con 'Re: ' garantizado), In-Reply-To y References
    para que el correo quede enhebrado en el hilo correcto. Función pura
    (sin red): testeable en aislamiento.
    """
    msg = EmailMessage()
    msg["To"] = to_addr
    msg["Subject"] = _ensure_re_subject(subject)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    # References debe encadenar el hilo: si no nos pasan nada pero hay
    # In-Reply-To, usamos ese id como References mínimo.
    refs = references or in_reply_to
    if refs:
        msg["References"] = refs
    # set_content fija Content-Type: text/plain; charset=utf-8 y el CTE.
    msg.set_content(body_text or "", subtype="plain", charset="utf-8")
    raw_bytes = msg.as_bytes()
    return base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")


# ---------------------------------------------------------------------------
# Helpers de parseo (puros: sin DB, sin red — testeables en aislamiento)
# ---------------------------------------------------------------------------


def _b64url_decode(data: str) -> bytes:
    """Decodifica el base64url de Gmail (sin padding) a bytes."""
    if not data:
        return b""
    # Gmail usa base64url y a veces omite el padding "=".
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _strip_html(html: str) -> str:
    """Strip MUY básico de HTML a texto plano (fallback cuando no hay
    text/plain). No pretende ser un renderer: quita tags, resuelve un par de
    entidades comunes y normaliza espacios."""
    # Elimina bloques que no son contenido visible.
    html = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", html)
    # <br> y </p>/</div> → saltos de línea para no pegar palabras.
    html = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", html)
    html = re.sub(r"(?i)</\s*(p|div|tr|li|h[1-6])\s*>", "\n", html)
    # Resto de tags fuera.
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    # Entidades HTML más comunes.
    replacements = {
        "&nbsp;": " ",
        "&amp;": "&",
        "&lt;": "<",
        "&gt;": ">",
        "&quot;": '"',
        "&#39;": "'",
        "&apos;": "'",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    # Normaliza espacios pero conserva saltos de línea.
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


# Patrones peligrosos a nivel de documento. Defensa en profundidad BARATA (sin
# nuevas dependencias): quitamos bloques que nunca deben llegar al navegador
# antes de almacenar el HTML. NO es el saneado real — ese lo hace el frontend
# con DOMPurify (lista blanca). Esto es solo la primera barrera para que ni
# siquiera viaje basura obvia hasta el cliente.
_HTML_DOC_DANGER_PATTERNS = [
    # <script>…</script> y <style>…</style> (con o sin cierre correcto).
    re.compile(r"(?is)<script\b.*?(?:</script>|$)"),
    re.compile(r"(?is)<style\b.*?(?:</style>|$)"),
    # <head>…</head> entero (suele traer <style>/<meta>/<base>).
    re.compile(r"(?is)<head\b.*?(?:</head>|$)"),
    # Tags vacíos peligrosos a nivel de documento.
    re.compile(r"(?is)<link\b[^>]*>"),
    re.compile(r"(?is)<meta\b[^>]*>"),
    re.compile(r"(?is)<base\b[^>]*>"),
    # Comentarios (incluye los condicionales de Outlook: <!--[if ...]> … <![endif]-->).
    re.compile(r"(?s)<!--.*?-->"),
]


def sanitize_email_html_document(html: str) -> str:
    """Primera barrera (no la definitiva) sobre el HTML CRUDO de un correo.

    Elimina con regex los bloques peligrosos a nivel de documento (script, style,
    head, link, meta, base y comentarios condicionales de Outlook). El saneado
    REAL —lista blanca de etiquetas/atributos, neutralización de on*, bloqueo de
    imágenes remotas— lo hace el frontend con DOMPurify. Aquí solo evitamos que
    viaje basura evidente hasta el navegador.

    Función PURA: testeable en aislamiento.
    """
    if not html:
        return html or ""
    out = html
    for pat in _HTML_DOC_DANGER_PATTERNS:
        out = pat.sub("", out)
    return out.strip()


# Patrones de cabecera de cita (historial citado). Conservadores: solo cortan
# si la línea ENTERA (tras strip) matchea uno de estos arranques de cita.
# Mejor dejar de más (no cortar) que comerse contenido real del cliente.
_QUOTE_HEADER_PATTERNS = [
    # "On Tue, 3 Jun 2026 ... wrote:" (Gmail/inglés). Puede partirse en 2 líneas,
    # por eso aceptamos que termine en "wrote:" o que la línea siga abierta con
    # "On ... <email>" y el "wrote:" caiga en la siguiente.
    re.compile(r"^on\s.+wrote:\s*$", re.IGNORECASE),
    re.compile(r"^on\s.+\bwrote:?\s*$", re.IGNORECASE),
    # "El 3 jun 2026, a las 10:00, Ana <ana@x> escribió:" (Gmail/español).
    re.compile(r"^el\s.+escribi[oó]:\s*$", re.IGNORECASE),
    # Outlook / clientes clásicos.
    re.compile(r"^-{2,}\s*original message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^-{2,}\s*mensaje original\s*-{2,}\s*$", re.IGNORECASE),
    # Bloque separador de Outlook (línea larga de guiones bajos).
    re.compile(r"^_{10,}\s*$"),
    # Cabecera de reenvío "From: ... " / "De: ..." que abre el bloque citado.
    re.compile(r"^(from|de):\s.+$", re.IGNORECASE),
    # "-----Mensaje reenviado-----" / "Forwarded message".
    re.compile(r"^-{2,}\s*forwarded message\s*-{2,}\s*$", re.IGNORECASE),
]


def clean_email_body(text: str) -> str:
    """Limpia el cuerpo de un correo de forma CONSERVADORA para quedarnos solo
    con el mensaje "nuevo" del remitente (lo que el agente debe leer/responder).

    Corta, en este orden, en la PRIMERA aparición de:
      1. La firma: una línea que sea exactamente "-- " (RFC 3676) o "--".
      2. El historial citado: primera línea que empiece por ">" o que matchee
         un patrón de cabecera de cita (On ... wrote: / El ... escribió: /
         -----Original Message----- / bloque de Outlook / From: ... ).
    Luego colapsa líneas en blanco múltiples y hace trim.

    Filosofía: mejor dejar de más que comerse contenido real. Si tras limpiar
    el resultado queda VACÍO (p. ej. un correo que era solo firma + cita),
    devuelve el texto ORIGINAL sin tocar (no perdemos el mensaje).

    Función PURA: sin DB, sin red — testeable en aislamiento.
    """
    if not text:
        return text

    lines = text.split("\n")
    cut_at: int | None = None
    for i, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        # 1) Firma estándar: "-- " (con espacio, RFC 3676) o "--" a secas.
        #    Comparamos contra la línea cruda para el caso "-- " (con espacio),
        #    y contra el strip para "--".
        if raw_line == "-- " or stripped == "--":
            cut_at = i
            break
        # 2) Historial citado: líneas citadas (">") o cabeceras de cita.
        if stripped.startswith(">"):
            cut_at = i
            break
        if any(p.match(stripped) for p in _QUOTE_HEADER_PATTERNS):
            cut_at = i
            break

    cleaned_lines = lines if cut_at is None else lines[:cut_at]
    cleaned = "\n".join(cleaned_lines)

    # 3) Colapsa 3+ saltos en como mucho 2, recorta espacios por línea al final
    #    y hace trim global.
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n\s*\n\s*\n+", "\n\n", cleaned)
    cleaned = cleaned.strip()

    # 4) Si nos quedamos sin nada (era solo firma/cita), no perdemos el mensaje:
    #    devolvemos el original tal cual.
    if not cleaned:
        return text

    return cleaned


# Marcador de adjunto que añade `parse_get_message_result` al final del cuerpo.
# Se reconoce para conservarlo al preparar el texto para el modelo (si el correo
# trae un presupuesto, el agente tiene que enterarse).
_ATTACHMENT_MARKER = re.compile(r"^\[adjunto:\s.+\]$")


def _strip_quoted_lines(text: str) -> str:
    """Quita las líneas CITADAS (las que empiezan por ">") y las cabeceras de
    cita, dejando el resto en su sitio.

    Es la variante para el BOTTOM-POSTING: cuando el cliente escribe DEBAJO de
    la cita, `clean_email_body` corta en la primera línea citada y se queda sin
    nada (por su red de seguridad devuelve el original entero). Aquí no
    cortamos: filtramos. Lo que sobrevive es lo que ha escrito la persona,
    arriba o abajo.

    Función PURA.
    """
    kept: list[str] = []
    for raw_line in text.split("\n"):
        stripped = raw_line.strip()
        if stripped.startswith(">"):
            continue
        if any(p.match(stripped) for p in _QUOTE_HEADER_PATTERNS):
            continue
        kept.append(raw_line)
    out = "\n".join(kept)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n\s*\n\s*\n+", "\n\n", out)
    return out.strip()


def truncate_email_for_agent(text: str, limit: int) -> str:
    """Recorta conservando PRINCIPIO Y FINAL, no solo el principio.

    Un recorte por el final (`text[:limit]`) se come la pregunta real en cuanto
    el cliente responde debajo de la cita — que es lo normal en Outlook. Como no
    se puede saber de antemano dónde está lo importante, nos quedamos con los
    dos extremos y marcamos el hueco.

    Función PURA.
    """
    if limit <= 0 or len(text) <= limit:
        return text
    marker = "\n\n[…parte central del correo recortada por longitud…]\n\n"
    room = limit - len(marker)
    if room <= 0:
        return text[:limit]
    head_len = room // 2
    tail_len = room - head_len
    return text[:head_len].rstrip() + marker + text[-tail_len:].lstrip()


def prepare_email_text_for_agent(text: str, limit: int = 0) -> str:
    """Deja el correo en lo que el AGENTE tiene que leer: el mensaje nuevo.

    Decisión (E7): el cuerpo COMPLETO se sigue guardando tal cual en la base de
    datos — la dueña quiere ver el correo entero en el panel y eso no se toca.
    Lo que se limpia es únicamente la copia que va al modelo, y por dos razones
    concretas: el historial citado y el aviso legal se llevaban casi todo el
    presupuesto de caracteres, y el recorte por longitud dejaba fuera la
    pregunta real cuando venía al final.

    Orden:
      1. `clean_email_body`: corta en la firma o en el arranque de la cita.
         Cubre el caso normal (respuesta ARRIBA) y está bien probada.
      2. Si eso no ha recortado nada (su red de seguridad devuelve el original
         cuando el resultado quedaría vacío: típico del BOTTOM-POSTING), se
         filtran las líneas citadas una a una. Así sobrevive lo que escribió la
         persona esté donde esté.
      3. Se vuelven a pegar los marcadores "[adjunto: …]" si la limpieza se los
         llevó: el agente tiene que saber que venía un fichero.
      4. Recorte final por los DOS extremos (ver `truncate_email_for_agent`).
         `limit=0` = sin recorte aquí (lo aplica el guardrail general).
    """
    if not text:
        return text

    markers = [
        line.strip()
        for line in text.split("\n")
        if _ATTACHMENT_MARKER.match(line.strip())
    ]

    cleaned = clean_email_body(text)
    if cleaned == text:
        # `clean_email_body` no ha tocado nada: o el correo era limpio, o su red
        # de seguridad devolvió el original porque el corte lo vaciaba todo.
        filtered = _strip_quoted_lines(text)
        if filtered:
            cleaned = filtered

    if markers:
        missing = [m for m in markers if m not in cleaned]
        if missing:
            cleaned = (cleaned + ("\n\n" if cleaned else "") + "\n".join(missing)).strip()

    if not cleaned.strip():
        # Nunca devolvemos vacío: mejor el correo entero que dejar al agente sin
        # nada que leer.
        cleaned = text

    return truncate_email_for_agent(cleaned, limit) if limit else cleaned


def _headers_to_dict(headers: list[dict[str, Any]]) -> dict[str, str]:
    """Lista de {name, value} de Gmail → dict case-insensitive (lower keys)."""
    out: dict[str, str] = {}
    for h in headers or []:
        name = (h.get("name") or "").lower()
        if name:
            out[name] = h.get("value") or ""
    return out


def _part_is_inline(headers: dict[str, str]) -> bool:
    """¿Esta parte con nombre de fichero es contenido INCRUSTADO (no un adjunto)?

    El logo de una firma corporativa viaja como parte MIME con filename, igual
    que un presupuesto en PDF. Distinguirlos importa: si no, el agente lee
    "[adjunto: logo-firma.png]" en cada correo y la operadora no sabe qué es de
    verdad un documento del cliente.

    Criterio (RFC 2183 + RFC 2392), en este orden:
      1. Content-Disposition: attachment  → adjunto REAL, mande lo que mande el
         resto (un PDF puede llevar Content-ID y seguir siendo un adjunto).
      2. Content-Disposition: inline      → incrustado.
      3. Content-ID presente              → incrustado (es lo que referencia el
         HTML con `src="cid:..."`: firmas, logos, iconos).
      4. Sin nada de lo anterior          → adjunto (no perdemos ficheros por
         un correo mal formado).
    """
    disposition = (headers.get("content-disposition") or "").strip().lower()
    if disposition.startswith("attachment"):
        return False
    if disposition.startswith("inline"):
        return True
    return bool(headers.get("content-id"))


def _extract_body_and_attachments(
    payload: dict[str, Any],
) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Recorre el árbol MIME del payload y devuelve (texto, html, adjuntos,
    incrustados).

    - `texto`: el mejor text/plain. Si no hay text/plain, se deriva del HTML con
      un strip básico (para que el agente y los canales sin HTML tengan algo).
    - `html`: el mejor (más largo) text/html CRUDO de las partes, o "" si no hay.
      El saneado real con lista blanca lo hace el frontend (DOMPurify); aquí solo
      lo extraemos y, en `parse_get_message_result`, le aplicamos una primera
      barrera regex a nivel de documento.
    - `adjuntos` / `incrustados`: partes con filename, separadas por
      `_part_is_inline`. De cada una se guarda lo necesario para poder
      DESCARGARLA después (`attachment_id` de la Gmail API, tamaño, tipo). Antes
      solo se guardaba el nombre, así que no había forma de bajar el fichero.
      El binario sigue sin descargarse aquí: se pide bajo demanda.
    """
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict[str, Any]] = []
    inline_parts: list[dict[str, Any]] = []

    def walk(part: dict[str, Any]) -> None:
        mime = (part.get("mimeType") or "").lower()
        filename = part.get("filename") or ""
        body = part.get("body") or {}
        sub_parts = part.get("parts") or []

        # Parte con filename → fichero. No bajamos el binario: guardamos su
        # ficha (incluido attachment_id) para poder pedirlo luego.
        if filename:
            part_headers = _headers_to_dict(part.get("headers") or [])
            entry = {
                "filename": filename,
                "mime_type": part.get("mimeType") or "application/octet-stream",
                "size": body.get("size"),
                # Identificador que pide users.messages.attachments.get. Puede
                # faltar si el binario viene embebido en `body.data` (raro).
                "attachment_id": body.get("attachmentId"),
                "part_id": part.get("partId"),
                "content_id": (part_headers.get("content-id") or "").strip("<>") or None,
            }
            if _part_is_inline(part_headers):
                entry["inline"] = True
                inline_parts.append(entry)
            else:
                entry["inline"] = False
                attachments.append(entry)
            # No bajamos por sus hijos ni leemos data.
            return

        if mime == "text/plain" and body.get("data"):
            try:
                plain_parts.append(_b64url_decode(body["data"]).decode("utf-8", errors="replace"))
            except Exception:
                pass
        elif mime == "text/html" and body.get("data"):
            try:
                html_parts.append(_b64url_decode(body["data"]).decode("utf-8", errors="replace"))
            except Exception:
                pass

        for sp in sub_parts:
            walk(sp)

    walk(payload or {})

    # Mejor parte HTML: nos quedamos con la más larga (suele ser el cuerpo real,
    # no una firma/tracking suelta). Se devuelve CRUDA; la sanea el frontend.
    best_html = max(html_parts, key=len) if html_parts else ""

    if plain_parts:
        body_text = "\n".join(p.strip() for p in plain_parts if p.strip()).strip()
    elif html_parts:
        body_text = _strip_html("\n".join(html_parts))
    else:
        body_text = ""

    return body_text, best_html, attachments, inline_parts


def parse_get_message_result(raw: dict[str, Any]) -> dict[str, Any]:
    """Normaliza la respuesta cruda de users.messages.get (format=full) a un
    dict estable que luego consume `parse_message_to_incoming`.

    Devuelve: id, threadId, from_name, from_addr, to_addr, subject,
    rfc822_message_id, in_reply_to, references, date, body, html, attachments.
    """
    payload = raw.get("payload") or {}
    headers = _headers_to_dict(payload.get("headers") or [])

    from_raw = headers.get("from", "")
    from_name, from_addr = parseaddr(from_raw)
    to_name, to_addr = parseaddr(headers.get("to", ""))

    body_text, html_raw, attachments, inline_attachments = _extract_body_and_attachments(
        payload
    )

    # Decisión de producto: NO recortamos el cuerpo por firma/cita. La dueña
    # quiere ver el correo COMPLETO (no perder contenido por heurísticas), así
    # que almacenamos el texto plano íntegro. `clean_email_body` permanece en el
    # módulo por si se necesita en otro contexto, pero NO se aplica aquí.
    # El agente también recibe el texto completo (revisa cada borrador, así que
    # el ruido del footer es aceptable).

    # HTML para visualización: primera barrera regex a nivel de documento
    # (script/style/head/link/meta/base/comentarios). El saneado real (lista
    # blanca, on*, imágenes remotas) lo hace el frontend con DOMPurify.
    html_clean = sanitize_email_html_document(html_raw)

    # Marcadores de adjunto al final del cuerpo de TEXTO (sin descarga: el
    # binario se pide bajo demanda con `get_attachment`). SOLO los adjuntos
    # REALES: los logos incrustados de una firma no se anuncian, que era lo que
    # llenaba de ruido lo que lee el agente.
    if attachments:
        markers = "\n".join(f"[adjunto: {a['filename']}]" for a in attachments)
        body_text = (body_text + ("\n\n" if body_text else "") + markers).strip()

    # Reply-To manda sobre From al responder (RFC 5322): un correo enviado desde
    # un sistema puede pedir explícitamente que se conteste a otra dirección.
    _, reply_to_addr = parseaddr(headers.get("reply-to", ""))

    return {
        "id": str(raw.get("id") or ""),
        "threadId": str(raw.get("threadId") or ""),
        "from_name": from_name or None,
        "from_addr": (from_addr or "").lower(),
        "to_addr": (to_addr or "").lower(),
        "reply_to_addr": (reply_to_addr or "").lower() or None,
        "subject": headers.get("subject") or None,
        "rfc822_message_id": headers.get("message-id") or None,
        "in_reply_to": headers.get("in-reply-to") or None,
        "references": headers.get("references") or None,
        "date": headers.get("date") or None,
        # Cabeceras de correo automático/masivo (newsletters, marketing,
        # notificaciones). Las usa el filtro determinista anti-tokens del runtime
        # (services/classifier.email_is_automated) para cuarentenar sin LLM.
        # None en correos personales normales.
        "list_unsubscribe": headers.get("list-unsubscribe") or None,
        "list_id": headers.get("list-id") or None,
        "precedence": headers.get("precedence") or None,
        "auto_submitted": headers.get("auto-submitted") or None,
        "body": body_text,
        # HTML crudo (ya pasado por la primera barrera). "" si el correo no
        # traía parte text/html.
        "html": html_clean,
        # Adjuntos REALES (fichas con attachment_id/tamaño/tipo, descargables).
        "attachments": attachments,
        # Partes incrustadas (logos de firma, iconos). Se guardan aparte por si
        # hiciera falta resolver un `cid:` del HTML, pero NO se anuncian al
        # agente ni cuentan como adjuntos del cliente.
        "inline_attachments": inline_attachments,
        # Etiquetas de Gmail (SENT, DRAFT, INBOX, ...). F5b las usa para
        # distinguir un correo ENVIADO (SENT) de un BORRADOR (DRAFT) al
        # sincronizar "Enviados" y NO ingerir los borradores del agente.
        "label_ids": list(raw.get("labelIds") or []),
    }


def parse_message_to_incoming(raw: dict[str, Any]) -> IncomingMessage:
    """Convierte un mensaje Gmail (ya normalizado por `parse_get_message_result`,
    o el dict crudo de get_message) en un `IncomingMessage`.

    Acepta tanto el dict normalizado como el crudo de la API: si detecta el
    crudo (tiene 'payload'), lo normaliza primero.
    """
    if "payload" in raw and "from_addr" not in raw:
        raw = parse_get_message_result(raw)

    sender_addr = raw.get("from_addr") or ""
    return IncomingMessage(
        provider_message_id=str(raw.get("id") or ""),
        # Identificador único del contacto para este canal: el email real,
        # prefijado con "email:" (el prefijo lo interpreta store_incoming).
        from_phone=f"email:{sender_addr}",
        to_phone=f"email:{raw.get('to_addr') or ''}",
        message_type="text",
        text=raw.get("body") or "",
        audio_url=None,
        audio_mime=None,
        customer_name=raw.get("from_name") or None,
        raw={
            "threadId": raw.get("threadId"),
            "subject": raw.get("subject"),
            "rfc822_message_id": raw.get("rfc822_message_id"),
            "in_reply_to": raw.get("in_reply_to"),
            "references": raw.get("references"),
            "attachments": raw.get("attachments") or [],
            "inline_attachments": raw.get("inline_attachments") or [],
            # Dirección a la que hay que RESPONDER de verdad (Reply-To si lo
            # hay, si no el From). No es lo mismo que el email del contacto en
            # el CRM, que alguien puede haber editado a mano.
            "reply_to_addr": raw.get("reply_to_addr") or raw.get("from_addr") or None,
            # HTML del correo (ya con la primera barrera regex). "" si no había
            # parte text/html. store_incoming lo guarda en Message.extra.html_body.
            "html": raw.get("html") or "",
            # Cabeceras para el filtro determinista de correo automático
            # (newsletters/marketing/notificaciones). store_incoming las guarda
            # en Message.extra.email_headers y el runtime las evalúa sin LLM.
            "email_headers": {
                "list_unsubscribe": raw.get("list_unsubscribe"),
                "list_id": raw.get("list_id"),
                "precedence": raw.get("precedence"),
                "auto_submitted": raw.get("auto_submitted"),
            },
        },
    )


# ---------------------------------------------------------------------------
# Cliente REST (necesita access_token → import diferido para no atar el
# parseo a la config/DB)
# ---------------------------------------------------------------------------


class GmailClient:
    """Cliente REST de Gmail para la cuenta conectada vía OAuth (google_gmail)."""

    async def _headers(self) -> dict[str, str] | None:
        from app.services.google_oauth import get_access_token

        token = await get_access_token(GMAIL_PROVIDER)
        if not token:
            return None
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def get_profile(self) -> dict[str, Any] | None:
        """users.getProfile → {emailAddress, messagesTotal, historyId}."""
        headers = await self._headers()
        if not headers:
            return None
        url = f"{GMAIL_BASE}/users/me/profile"
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
        return {
            "email_address": (data.get("emailAddress") or "").lower(),
            "history_id": str(data.get("historyId") or ""),
            "messages_total": data.get("messagesTotal"),
        }

    async def list_history(self, start_history_id: str) -> dict[str, Any]:
        """users.history.list con historyTypes=messageAdded.

        Devuelve {"message_ids": [...], "history_id": "<nuevo>", "expired": bool}.
        `expired=True` si Gmail responde 404 (el start_history_id ya caducó y hay
        que re-anclar con get_profile). Maneja paginación con pageToken.
        """
        headers = await self._headers()
        if not headers:
            return {"message_ids": [], "history_id": start_history_id, "expired": False}

        url = f"{GMAIL_BASE}/users/me/history"
        message_ids: list[str] = []
        latest_history_id = start_history_id
        page_token: str | None = None

        async with httpx.AsyncClient(timeout=20.0) as client:
            while True:
                params: dict[str, Any] = {
                    "startHistoryId": start_history_id,
                    "historyTypes": "messageAdded",
                }
                if page_token:
                    params["pageToken"] = page_token
                r = await client.get(url, headers=headers, params=params)
                if r.status_code == 404:
                    # historyId caducado → señalamos para re-bootstrap.
                    return {"message_ids": [], "history_id": start_history_id, "expired": True}
                r.raise_for_status()
                data = r.json()

                if data.get("historyId"):
                    latest_history_id = str(data["historyId"])
                for h in data.get("history") or []:
                    for added in h.get("messagesAdded") or []:
                        msg = added.get("message") or {}
                        mid = msg.get("id")
                        if mid:
                            message_ids.append(str(mid))

                page_token = data.get("nextPageToken")
                if not page_token:
                    break

        # Dedup conservando orden (un mismo mensaje puede aparecer en varios
        # registros de history).
        seen: set[str] = set()
        deduped = [m for m in message_ids if not (m in seen or seen.add(m))]
        return {"message_ids": deduped, "history_id": latest_history_id, "expired": False}

    async def list_messages(self, query: str, max_results: int = 50) -> list[str]:
        """users.messages.list?q=<query> → lista de message_ids (bootstrap).

        Maneja paginación hasta agotar resultados o llegar a max_results.
        """
        headers = await self._headers()
        if not headers:
            return []
        url = f"{GMAIL_BASE}/users/me/messages"
        ids: list[str] = []
        page_token: str | None = None
        async with httpx.AsyncClient(timeout=20.0) as client:
            while len(ids) < max_results:
                params: dict[str, Any] = {"q": query, "maxResults": min(100, max_results)}
                if page_token:
                    params["pageToken"] = page_token
                r = await client.get(url, headers=headers, params=params)
                r.raise_for_status()
                data = r.json()
                for m in data.get("messages") or []:
                    if m.get("id"):
                        ids.append(str(m["id"]))
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
        return ids[:max_results]

    async def get_message(self, message_id: str) -> dict[str, Any] | None:
        """users.messages.get?format=full, ya parseado a dict normalizado."""
        headers = await self._headers()
        if not headers:
            return None
        url = f"{GMAIL_BASE}/users/me/messages/{message_id}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(url, headers=headers, params={"format": "full"})
            r.raise_for_status()
            data = r.json()
        return parse_get_message_result(data)

    async def get_attachment(self, message_id: str, attachment_id: str) -> bytes | None:
        """users.messages.attachments.get → binario del adjunto.

        Devuelve los bytes ya decodificados, o None si no hay credenciales.
        Lanza si Gmail responde error (el llamante decide qué contarle al
        operador). No guardamos el fichero: Gmail es el archivo, esto es una
        descarga bajo demanda.
        """
        headers = await self._headers()
        if not headers:
            return None
        url = f"{GMAIL_BASE}/users/me/messages/{message_id}/attachments/{attachment_id}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, headers=headers)
            if r.status_code >= 400:
                logger.error(
                    "gmail.get_attachment.failed",
                    status=r.status_code,
                    message_id=message_id,
                )
                r.raise_for_status()
            data = r.json()
        return _b64url_decode(data.get("data") or "")

    async def send_message(
        self,
        thread_id: str,
        to_addr: str,
        subject: str,
        body_text: str,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> dict[str, Any]:
        """users.messages.send → envía un correo DE VERDAD en el hilo `thread_id`
        (F5b). Es la respuesta manual libre de la operadora desde el panel, fuera
        del flujo de borradores del agente.

        Construye el MIME con `build_raw_message` (Subject con 'Re: ' garantizado,
        To, In-Reply-To y References para enhebrar correctamente; text/plain
        UTF-8; base64url) y hace POST con {raw, threadId}.

        Devuelve {"message_id", "thread_id"}. Lanza si Gmail responde error. No
        volcamos el cuerpo en logs (minimización de PII): solo status y longitud.
        """
        headers = await self._headers()
        if not headers:
            raise RuntimeError("Gmail: sin credenciales (token no disponible)")
        raw = build_raw_message(to_addr, subject, body_text, in_reply_to, references)
        payload: dict[str, Any] = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id
        url = f"{GMAIL_BASE}/users/me/messages/send"
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(url, headers=headers, json=payload)
            if r.status_code >= 400:
                logger.error("gmail.send_message.failed", status=r.status_code, body_len=len(body_text or ""))
                r.raise_for_status()
            data = r.json()
        return {
            "message_id": str(data.get("id") or ""),
            "thread_id": str(data.get("threadId") or ""),
        }

    # ------------------------------------------------------------------
    # Borradores (F5c). El agente NUNCA envía: crea un borrador para revisión.
    # Requieren scope gmail.modify (ya concedido). No volcamos el cuerpo del
    # correo en logs (minimización de PII): solo ids y longitudes.
    # ------------------------------------------------------------------

    async def create_draft(
        self,
        thread_id: str,
        to_addr: str,
        subject: str,
        body_text: str,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> dict[str, Any]:
        """users.drafts.create → crea un borrador REAL en el hilo `thread_id`.

        Devuelve {"draft_id", "message_id"}. Lanza si Gmail responde error.
        """
        headers = await self._headers()
        if not headers:
            raise RuntimeError("Gmail: sin credenciales (token no disponible)")
        raw = build_raw_message(to_addr, subject, body_text, in_reply_to, references)
        payload: dict[str, Any] = {"message": {"raw": raw}}
        if thread_id:
            payload["message"]["threadId"] = thread_id
        url = f"{GMAIL_BASE}/users/me/drafts"
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(url, headers=headers, json=payload)
            if r.status_code >= 400:
                logger.error("gmail.create_draft.failed", status=r.status_code, body_len=len(body_text or ""))
                r.raise_for_status()
            data = r.json()
        msg = data.get("message") or {}
        return {"draft_id": str(data.get("id") or ""), "message_id": str(msg.get("id") or "")}

    async def update_draft(
        self,
        draft_id: str,
        thread_id: str,
        to_addr: str,
        subject: str,
        body_text: str,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> dict[str, Any]:
        """users.drafts.update (PUT) → re-escribe el raw del borrador existente.

        Devuelve {"draft_id", "message_id"}.
        """
        headers = await self._headers()
        if not headers:
            raise RuntimeError("Gmail: sin credenciales (token no disponible)")
        raw = build_raw_message(to_addr, subject, body_text, in_reply_to, references)
        payload: dict[str, Any] = {"message": {"raw": raw}}
        if thread_id:
            payload["message"]["threadId"] = thread_id
        url = f"{GMAIL_BASE}/users/me/drafts/{draft_id}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.put(url, headers=headers, json=payload)
            if r.status_code >= 400:
                logger.error("gmail.update_draft.failed", status=r.status_code, draft_id=draft_id)
                r.raise_for_status()
            data = r.json()
        msg = data.get("message") or {}
        return {"draft_id": str(data.get("id") or draft_id), "message_id": str(msg.get("id") or "")}

    async def send_draft(self, draft_id: str) -> dict[str, Any]:
        """users.drafts.send → envía el borrador. Devuelve {"message_id",
        "thread_id"} del mensaje enviado."""
        headers = await self._headers()
        if not headers:
            raise RuntimeError("Gmail: sin credenciales (token no disponible)")
        url = f"{GMAIL_BASE}/users/me/drafts/send"
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(url, headers=headers, json={"id": draft_id})
            if r.status_code >= 400:
                logger.error("gmail.send_draft.failed", status=r.status_code, draft_id=draft_id)
                r.raise_for_status()
            data = r.json()
        return {
            "message_id": str(data.get("id") or ""),
            "thread_id": str(data.get("threadId") or ""),
        }

    async def delete_draft(self, draft_id: str) -> bool:
        """users.drafts.delete → borra el borrador (best-effort, para limpieza).

        Devuelve True si Gmail lo aceptó (204) o ya no existía (404). No lanza:
        es limpieza, no queremos romper el flujo del operador por esto."""
        headers = await self._headers()
        if not headers:
            return False
        url = f"{GMAIL_BASE}/users/me/drafts/{draft_id}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.delete(url, headers=headers)
            if r.status_code in (200, 204, 404):
                return True
            logger.warning("gmail.delete_draft.unexpected", status=r.status_code, draft_id=draft_id)
            return False
        except Exception as e:
            logger.warning("gmail.delete_draft.failed", draft_id=draft_id, error=str(e))
            return False

    # ------------------------------------------------------------------
    # Etiquetas y archivado (requieren scope gmail.modify, ya concedido).
    # Sirven para que lo que el panel descarta se archive y se etiquete
    # TAMBIÉN en el buzón real, como haría un filtro de Gmail.
    # ------------------------------------------------------------------

    @staticmethod
    def _find_user_label(labels: list[dict[str, Any]], name: str) -> str | None:
        """Busca una etiqueta PROPIA por nombre. Nunca devuelve una de sistema.

        Gmail llama a sus etiquetas de sistema por su nombre en inglés —"SPAM",
        "TRASH", "INBOX"— y devuelve `type: "system"` para distinguirlas. Sin
        ese filtro, poner "Spam" en la caja de la etiqueta del panel hacía que
        el archivado marcara el correo como spam de verdad (y Gmail lo borra a
        los 30 días); con "Inbox", el modify pedía añadir y quitar INBOX a la
        vez y fallaba en silencio. La API además rechaza crear una etiqueta con
        uno de esos nombres, así que este camino tampoco puede inventarlas.
        """
        for label in labels or []:
            if (label.get("type") or "user") != "user":
                continue
            if (label.get("name") or "").lower() == name.lower():
                return str(label.get("id") or "") or None
        return None

    async def ensure_label(self, name: str) -> str | None:
        """Devuelve el id de la etiqueta `name`, creándola si no existe.

        CACHEADO. Antes esto pedía la lista ENTERA de etiquetas del buzón en
        cada correo que se archivaba, para acabar devolviendo siempre el mismo
        identificador de una etiqueta que se crea una vez y no cambia nunca. El
        id vive ahora en Redis, compartido por todos los workers, y se tira
        solo si Gmail lo rechaza —lo que pasa si alguien borra la etiqueta a
        mano— o si se cambia el nombre desde el panel.

        None si no hay credenciales, si Gmail falla o si el nombre choca con
        una etiqueta de sistema: el llamante lo trata como "no se pudo
        etiquetar", nunca como un error que corte el flujo.
        """
        headers = await self._headers()
        if not headers:
            return None
        cacheado = await label_id_cacheado(name)
        if cacheado:
            return cacheado
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(f"{GMAIL_BASE}/users/me/labels", headers=headers)
                r.raise_for_status()
                existente = self._find_user_label(r.json().get("labels") or [], name)
                if existente:
                    await _guardar_label_id(name, existente)
                    return existente
                # No existe: se crea visible en la lista y en los mensajes, para
                # poder pinchar en Gmail y ver qué se ha descartado.
                r = await client.post(
                    f"{GMAIL_BASE}/users/me/labels",
                    headers=headers,
                    json={
                        "name": name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                )
                # 409 = ya existía (dos workers a la vez): se relee y se coge.
                if r.status_code == 409:
                    r2 = await client.get(f"{GMAIL_BASE}/users/me/labels", headers=headers)
                    r2.raise_for_status()
                    creada = self._find_user_label(r2.json().get("labels") or [], name)
                    if creada:
                        await _guardar_label_id(name, creada)
                    return creada
                r.raise_for_status()
                nueva = str(r.json().get("id") or "") or None
                if nueva:
                    await _guardar_label_id(name, nueva)
                return nueva
        except Exception as e:
            logger.warning("gmail.ensure_label.failed", label=name, error=str(e))
            return None

    async def modify_message(
        self, message_id: str, add: list[str] | None = None, remove: list[str] | None = None
    ) -> bool:
        """users.messages.modify → añade/quita etiquetas de un mensaje.

        Quitar "INBOX" es exactamente lo que Gmail llama archivar. No borra nada
        ni toca "SPAM": lo descartado sigue estando, solo que fuera de Recibidos
        y con su etiqueta.
        """
        if not message_id or (not add and not remove):
            return False
        headers = await self._headers()
        if not headers:
            return False
        payload = {"addLabelIds": add or [], "removeLabelIds": remove or []}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(
                    f"{GMAIL_BASE}/users/me/messages/{message_id}/modify",
                    headers=headers,
                    json=payload,
                )
            if r.status_code >= 400:
                logger.warning(
                    "gmail.modify_message.failed", status=r.status_code, message_id=message_id
                )
                return False
            return True
        except Exception as e:
            logger.warning("gmail.modify_message.error", message_id=message_id, error=str(e))
            return False

    async def batch_modify_messages(
        self,
        message_ids: list[str],
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> bool:
        """users.messages.batchModify → la misma etiqueta a varios de una vez.

        Un hilo de correo descartado suele traer varios mensajes, y cada uno
        salía en su propia llamada HTTP a Gmail, en serie, con el cerrojo de la
        conversación cogido. Esto lo hace en una.

        Gmail responde 204 SIN CUERPO: no dice qué mensaje sí y cuál no, es
        todo o nada. Por eso el llamante se guarda el camino de uno en uno para
        cuando esto falla — así un identificador malo no se lleva por delante
        el archivado de los demás.
        """
        ids = [m for m in (message_ids or []) if m]
        if not ids or (not add and not remove):
            return False
        headers = await self._headers()
        if not headers:
            return False
        payload = {"ids": ids, "addLabelIds": add or [], "removeLabelIds": remove or []}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(
                    f"{GMAIL_BASE}/users/me/messages/batchModify",
                    headers=headers,
                    json=payload,
                )
            if r.status_code >= 400:
                logger.warning(
                    "gmail.batch_modify.failed", status=r.status_code, mensajes=len(ids)
                )
                return False
            return True
        except Exception as e:
            logger.warning("gmail.batch_modify.error", mensajes=len(ids), error=str(e))
            return False


# --------------------------------------------------------------------------
# Caché del id de la etiqueta de archivado.
#
# Es un dato que se crea una vez y no cambia: la etiqueta la crea este mismo
# código la primera vez y ahí se queda. Pedirla a Gmail en cada correo era una
# llamada de red entera —la lista COMPLETA de etiquetas del buzón— para acabar
# devolviendo siempre lo mismo.
#
# La caché se tira sola cuando Gmail rechaza el id (etiqueta borrada a mano) y
# cuando se cambia el nombre en el panel. Y aunque no se tirara, un id de
# etiqueta que ya no existe solo hace que el archivado falle y quede en el log,
# nunca que se toque el correo equivocado.
# --------------------------------------------------------------------------

_LABEL_CACHE_KEY = "gmail:label_id:{name}"
_LABEL_CACHE_TTL_S = 6 * 3600


async def label_id_cacheado(name: str) -> str | None:
    try:
        valor = await get_redis().get(_LABEL_CACHE_KEY.format(name=name.lower()))
    except Exception:  # noqa: BLE001 — sin Redis se pregunta a Gmail
        return None
    return str(valor) if valor else None


async def _guardar_label_id(name: str, label_id: str) -> None:
    try:
        await get_redis().set(
            _LABEL_CACHE_KEY.format(name=name.lower()), label_id, ex=_LABEL_CACHE_TTL_S
        )
    except Exception:  # noqa: BLE001 — no cachear no es un error
        pass


async def olvidar_label_id(name: str) -> None:
    """Tira el id cacheado de esa etiqueta.

    La llaman el archivado cuando Gmail rechaza el id (alguien borró la
    etiqueta) y el panel cuando se cambia el nombre.
    """
    try:
        await get_redis().delete(_LABEL_CACHE_KEY.format(name=name.lower()))
    except Exception:  # noqa: BLE001
        pass


_client_singleton: GmailClient | None = None


def get_gmail_provider() -> GmailClient:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = GmailClient()
    return _client_singleton
