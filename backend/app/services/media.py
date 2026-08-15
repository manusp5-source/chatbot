"""Validación y storage local de adjuntos salientes del operador.

WhatsApp Business API solo acepta ciertos MIME types por categoría y con
ciertos tamaños máximos. Si subimos algo fuera de spec, YCloud rechaza el
send (a veces con un 400 oscuro), así que validamos antes de subir.
"""
from __future__ import annotations

import mimetypes
import os
import uuid
from pathlib import Path

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


# MIME types aceptados por WhatsApp Business API (subset razonable, sin
# tipos exóticos que sufren rate de rechazo alto).
# Referencias:
# https://developers.facebook.com/docs/whatsapp/cloud-api/reference/media
_IMAGE_MIMES = {"image/jpeg", "image/jpg", "image/png"}
_AUDIO_MIMES = {
    "audio/aac",
    "audio/mp4",
    "audio/mpeg",
    "audio/amr",
    "audio/ogg",
    "audio/ogg; codecs=opus",
    "audio/opus",
    # Browsers (MediaRecorder) suelen producir webm con opus — YCloud lo
    # acepta tras transcodificar internamente; lo dejamos para no romper UX.
    "audio/webm",
    "audio/webm; codecs=opus",
}
_VIDEO_MIMES = {"video/mp4", "video/3gpp"}
_DOCUMENT_MIMES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/plain",
    "text/csv",
}
_STICKER_MIMES = {"image/webp"}


class MediaValidationError(ValueError):
    """Levantado cuando el archivo no cumple los límites de WhatsApp."""


def categorize_mime(mime: str) -> str:
    """Devuelve la categoría WhatsApp para un MIME dado.

    Lanza MediaValidationError si el tipo no es válido para envío por WA.
    """
    m = (mime or "").lower().split(";")[0].strip()
    # Re-añadir el suffix para los casos como audio/ogg; codecs=opus
    full = (mime or "").lower().strip()
    if m in _IMAGE_MIMES:
        return "image"
    if m in _AUDIO_MIMES or full in _AUDIO_MIMES:
        return "audio"
    if m in _VIDEO_MIMES:
        return "video"
    if m in _DOCUMENT_MIMES:
        return "document"
    if m in _STICKER_MIMES:
        return "sticker"
    raise MediaValidationError(f"Tipo de archivo no soportado por WhatsApp: {mime}")


def max_bytes_for(media_kind: str) -> int:
    """Tope en bytes de esa categoría.

    Se expone aparte de `validate_size` para poder acotar la LECTURA del cuerpo
    de la petición, no solo comprobarla después: leer entero y preguntar luego
    significa que un cuerpo de varios GB ya está en memoria cuando se rechaza.
    """
    limits = {
        "image": settings.MEDIA_MAX_IMAGE_BYTES,
        "audio": settings.MEDIA_MAX_AUDIO_BYTES,
        "video": settings.MEDIA_MAX_VIDEO_BYTES,
        "document": settings.MEDIA_MAX_DOCUMENT_BYTES,
        "sticker": settings.MEDIA_MAX_STICKER_BYTES,
    }
    max_bytes = limits.get(media_kind)
    if max_bytes is None:
        raise MediaValidationError(f"Categoría desconocida: {media_kind}")
    return int(max_bytes)


def validate_size(media_kind: str, size_bytes: int) -> None:
    """Lanza MediaValidationError si el tamaño excede el límite para esa categoría."""
    max_bytes = max_bytes_for(media_kind)
    if size_bytes > max_bytes:
        mb = max_bytes / 1024 / 1024
        raise MediaValidationError(
            f"Archivo demasiado grande para {media_kind} (máx {mb:.0f} MB en WhatsApp)"
        )


def save_local(file_bytes: bytes, original_filename: str) -> tuple[str, str]:
    """Guarda el archivo bajo UPLOADS_PATH con un nombre único.

    Devuelve (filename_seguro, ruta_relativa_servible).
    `ruta_relativa_servible` es la que va a la BD y la sirve `/uploads/{filename}`.
    """
    Path(settings.UPLOADS_PATH).mkdir(parents=True, exist_ok=True)
    # Extensión del nombre original (si tiene), saneada.
    ext = ""
    if "." in original_filename:
        ext = original_filename.rsplit(".", 1)[-1].lower()
        # Solo caracteres alfanuméricos en la extensión (anti path traversal).
        ext = "".join(c for c in ext if c.isalnum())[:6]
        if ext:
            ext = "." + ext
    safe_name = f"{uuid.uuid4().hex}{ext}"
    abs_path = os.path.join(settings.UPLOADS_PATH, safe_name)
    with open(abs_path, "wb") as f:
        f.write(file_bytes)
    return safe_name, f"/uploads/{safe_name}"


def max_bytes_for_kind(media_kind: str) -> int:
    """Tope de bytes que aceptamos descargar para esa categoría."""
    limits = {
        "image": settings.MEDIA_MAX_IMAGE_BYTES,
        "audio": settings.MEDIA_MAX_AUDIO_BYTES,
        "video": settings.MEDIA_MAX_VIDEO_BYTES,
        "document": settings.MEDIA_MAX_DOCUMENT_BYTES,
        "sticker": settings.MEDIA_MAX_STICKER_BYTES,
    }
    return limits.get(media_kind, settings.MEDIA_MAX_IMAGE_BYTES)


def kind_from_mime(mime: str | None, fallback: str) -> str:
    """Categoría a partir del MIME real que devolvió el proveedor.

    La categoría que anuncia el webhook a veces miente (una historia citada
    puede ser vídeo y llegar como `story_mention`). Si el MIME no dice nada
    útil, nos quedamos con la que traía el webhook.
    """
    m = (mime or "").lower().split(";")[0].strip()
    if m.startswith("image/"):
        return "sticker" if m == "image/webp" and fallback == "sticker" else "image"
    if m.startswith("video/"):
        return "video"
    if m.startswith("audio/"):
        return "audio"
    if m and not m.startswith("text/html"):
        return "document" if fallback not in ("image", "video", "sticker") else fallback
    return fallback


async def fetch_incoming_media(
    *, canal: str, media_kind: str, media_url: str, media_filename: str | None
) -> dict | None:
    """Descarga un adjunto entrante del proveedor y lo guarda en el volumen.

    Las URLs de Meta/YCloud CADUCAN: si solo guardáramos el enlace, a los pocos
    días el histórico mostraría un hueco. Por eso bajamos una copia, igual que
    con las notas de voz.

    Devuelve los campos listos para el Message (media_type, media_url local,
    media_mime, media_size, media_filename) o None si no se pudo descargar. Es
    best-effort a propósito: un fallo aquí NO debe tumbar la ingesta del
    mensaje — el mensaje se guarda igual y en la bandeja se ve que llegó un
    adjunto, aunque no se pueda abrir.
    """
    max_bytes = max_bytes_for_kind(media_kind)
    try:
        if canal == "instagram_dm":
            from app.providers.instagram.meta import get_instagram_provider

            content, mime = await get_instagram_provider().download_media(
                media_url, max_bytes=max_bytes
            )
        else:
            from app.providers.whatsapp.ycloud import get_whatsapp_provider

            provider = get_whatsapp_provider()
            download = getattr(provider, "download_media", None)
            if download is None:
                return None
            content, mime = await download(media_url, max_bytes=max_bytes)
    except Exception as e:  # noqa: BLE001 — best-effort, ver docstring
        logger.warning("media.incoming.download_failed", canal=canal, error=str(e))
        return None

    if not content:
        return None
    kind = kind_from_mime(mime, media_kind)
    ext = mimetypes.guess_extension((mime or "").split(";")[0].strip()) or ""
    name = media_filename or f"adjunto{ext}"
    try:
        _safe_name, served_url = save_local(content, name)
    except OSError as e:
        logger.warning("media.incoming.save_failed", error=str(e))
        return None
    return {
        "media_type": kind,
        "media_url": served_url,
        "media_mime": (mime or "").split(";")[0].strip() or None,
        "media_size": len(content),
        "media_filename": media_filename,
    }


def resolve_local_path(filename: str) -> str | None:
    """Devuelve la ruta absoluta si el filename es seguro y existe.

    Bloquea path traversal (no permitimos `/` ni `..`).
    """
    if "/" in filename or "\\" in filename or ".." in filename:
        return None
    abs_path = os.path.join(settings.UPLOADS_PATH, filename)
    # Defensa adicional: garantizar que la ruta resuelta sigue dentro de
    # UPLOADS_PATH (defensa contra symlinks raros).
    try:
        real_uploads = os.path.realpath(settings.UPLOADS_PATH)
        real_target = os.path.realpath(abs_path)
        if not real_target.startswith(real_uploads + os.sep) and real_target != real_uploads:
            return None
    except OSError:
        return None
    if not os.path.isfile(abs_path):
        return None
    return abs_path
