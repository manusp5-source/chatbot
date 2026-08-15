"""Adjuntos entrantes que NO son nota de voz (imagen, vídeo, documento).

Antes se ignoraban: el mensaje se guardaba con contenido NULL y llegaba a la
bandeja como una burbuja COMPLETAMENTE en blanco — ni se veía la foto ni ponía
que hubiera llegado una. Aquí verificamos el parseo de los dos proveedores y la
descarga/guardado local (las URLs del proveedor caducan).

Tests puros (sin DB), al estilo de test_instagram_audio.py.
"""
import pytest

from app.providers.instagram.meta import InstagramProvider
from app.providers.whatsapp.ycloud import YCloudProvider
from app.services import media as media_service


def _ig_payload(*events: dict) -> dict:
    return {"object": "instagram", "entry": [{"id": "BIZ", "messaging": list(events)}]}


def _ig_attachment_event(mid: str, att_type: str, url: str, text: str | None = None) -> dict:
    msg: dict = {"mid": mid, "attachments": [{"type": att_type, "payload": {"url": url}}]}
    if text:
        msg["text"] = text
    return {
        "sender": {"id": "CUST1"},
        "recipient": {"id": "BIZ"},
        "timestamp": 1700000000000,
        "message": msg,
    }


# --- Instagram ------------------------------------------------------------


def test_ig_image_attachment_is_captured():
    p = InstagramProvider()
    url = "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=1&signature=x"
    m = p.parse_webhook(_ig_payload(_ig_attachment_event("mid.I1", "image", url)))[0]
    assert m.message_type == "image"
    assert m.media_kind == "image"
    assert m.media_url == url
    assert m.audio_url is None


def test_ig_video_and_file_map_to_their_kind():
    p = InstagramProvider()
    url = "https://lookaside.fbsbx.com/v/1"
    video = p.parse_webhook(_ig_payload(_ig_attachment_event("mid.V", "video", url)))[0]
    doc = p.parse_webhook(_ig_payload(_ig_attachment_event("mid.D", "file", url)))[0]
    assert video.media_kind == "video"
    assert doc.media_kind == "document"


def test_ig_image_with_caption_keeps_the_text():
    """El pie de foto es texto del cliente: el agente debe verlo."""
    p = InstagramProvider()
    url = "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=2"
    m = p.parse_webhook(
        _ig_payload(_ig_attachment_event("mid.I2", "image", url, text="mira esto"))
    )[0]
    assert m.media_kind == "image"
    assert m.text == "mira esto"


def test_ig_audio_has_priority_over_other_attachments():
    """Si el mensaje trae audio, manda el audio (su pipeline es la transcripción)."""
    p = InstagramProvider()
    ev = {
        "sender": {"id": "CUST1"},
        "recipient": {"id": "BIZ"},
        "timestamp": 1700000000000,
        "message": {
            "mid": "mid.MIX",
            "attachments": [
                {"type": "image", "payload": {"url": "https://x.fbcdn.net/i.jpg"}},
                {"type": "audio", "payload": {"url": "https://x.fbcdn.net/a.ogg"}},
            ],
        },
    }
    m = p.parse_webhook(_ig_payload(ev))[0]
    assert m.message_type == "audio"
    assert m.audio_url.endswith("a.ogg")


def test_ig_attachment_without_url_stays_other():
    """Sin URL no hay nada que descargar: no inventamos una categoría."""
    p = InstagramProvider()
    ev = {
        "sender": {"id": "CUST1"},
        "recipient": {"id": "BIZ"},
        "timestamp": 1700000000000,
        "message": {"mid": "mid.NO", "attachments": [{"type": "image", "payload": {}}]},
    }
    m = p.parse_webhook(_ig_payload(ev))[0]
    assert m.message_type == "other"
    assert m.media_kind is None


def test_ig_parse_does_not_clobber_the_payload_argument():
    """Regresión: el bucle de adjuntos pisaba la variable `payload` de la
    función. Con dos eventos en el mismo webhook, el segundo se perdía."""
    p = InstagramProvider()
    url = "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=3"
    out = p.parse_webhook(
        _ig_payload(
            _ig_attachment_event("mid.A", "image", url),
            _ig_attachment_event("mid.B", "image", url),
        )
    )
    assert [m.provider_message_id for m in out] == ["mid.A", "mid.B"]


# --- WhatsApp -------------------------------------------------------------


def _wa_payload(mtype: str, body: dict) -> dict:
    return {
        "type": "whatsapp.inbound_message.received",
        "whatsappInboundMessage": {
            "id": "wamid.M1",
            "from": "+34600000000",
            "to": "+34900000000",
            "type": mtype,
            mtype: body,
        },
    }


def test_wa_image_is_captured_with_caption():
    p = YCloudProvider()
    m = p.parse_webhook(
        _wa_payload(
            "image",
            {
                "link": "https://media.ycloud.com/i.jpg",
                "mime_type": "image/jpeg",
                "caption": "el ticket",
            },
        )
    )[0]
    assert m.message_type == "image"
    assert m.media_kind == "image"
    assert m.media_url == "https://media.ycloud.com/i.jpg"
    assert m.media_mime == "image/jpeg"
    assert m.text == "el ticket"


def test_wa_document_keeps_filename():
    p = YCloudProvider()
    m = p.parse_webhook(
        _wa_payload(
            "document",
            {
                "link": "https://media.ycloud.com/f.pdf",
                "mime_type": "application/pdf",
                "filename": "factura.pdf",
            },
        )
    )[0]
    assert m.media_kind == "document"
    assert m.media_filename == "factura.pdf"


# --- Descarga y guardado --------------------------------------------------


def test_kind_from_mime_corrects_the_webhook_category():
    """Una historia citada llega como `story_mention` pero puede ser vídeo."""
    assert media_service.kind_from_mime("video/mp4", "image") == "video"
    assert media_service.kind_from_mime("image/jpeg", "image") == "image"
    # Sin MIME útil nos quedamos con lo que dijo el webhook.
    assert media_service.kind_from_mime(None, "image") == "image"
    assert media_service.kind_from_mime("", "document") == "document"


@pytest.mark.asyncio
async def test_fetch_incoming_media_saves_a_local_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(media_service.settings, "UPLOADS_PATH", str(tmp_path))

    class _FakeProvider:
        async def download_media(self, url, *, max_bytes):
            return b"\xff\xd8\xff-imagen", "image/jpeg"

    monkeypatch.setattr(
        "app.providers.instagram.meta.get_instagram_provider", lambda: _FakeProvider()
    )
    out = await media_service.fetch_incoming_media(
        canal="instagram_dm",
        media_kind="image",
        media_url="https://lookaside.fbsbx.com/x",
        media_filename=None,
    )
    assert out["media_type"] == "image"
    assert out["media_url"].startswith("/uploads/")
    assert out["media_mime"] == "image/jpeg"
    assert out["media_size"] == len(b"\xff\xd8\xff-imagen")
    # El fichero está de verdad en el volumen.
    saved = tmp_path / out["media_url"].removeprefix("/uploads/")
    assert saved.read_bytes() == b"\xff\xd8\xff-imagen"


@pytest.mark.asyncio
async def test_fetch_incoming_media_returns_none_when_download_fails(monkeypatch):
    """Si la descarga peta, el mensaje debe guardarse igual (sin fichero)."""

    class _BrokenProvider:
        async def download_media(self, url, *, max_bytes):
            raise RuntimeError("Meta devolvió 403")

    monkeypatch.setattr(
        "app.providers.instagram.meta.get_instagram_provider", lambda: _BrokenProvider()
    )
    out = await media_service.fetch_incoming_media(
        canal="instagram_dm",
        media_kind="image",
        media_url="https://lookaside.fbsbx.com/x",
        media_filename=None,
    )
    assert out is None
