"""Instagram: parseo de notas de voz entrantes (transcripción).

Igual que WhatsApp, una nota de voz entra como adjunto type=audio y debe
producir un IncomingMessage(message_type="audio", audio_url=...). El resto del
pipeline (Whisper) ya es agnóstico al canal; aquí solo verificamos el parseo.

Tests puros (sin DB), al estilo de test_instagram_echo.py.
"""
from app.providers.instagram.meta import InstagramProvider


def _audio_event(mid: str, customer_psid: str, url: str, att_type: str = "audio") -> dict:
    """Evento entrante con un adjunto de audio (nota de voz)."""
    return {
        "sender": {"id": customer_psid},
        "recipient": {"id": "BIZ"},
        "timestamp": 1700000000000,
        "message": {
            "mid": mid,
            "attachments": [{"type": att_type, "payload": {"url": url}}],
        },
    }


def _text_event(mid: str, customer_psid: str, text: str = "buenas") -> dict:
    return {
        "sender": {"id": customer_psid},
        "recipient": {"id": "BIZ"},
        "timestamp": 1700000000000,
        "message": {"mid": mid, "text": text},
    }


def _payload(*events: dict) -> dict:
    return {"object": "instagram", "entry": [{"id": "BIZ", "messaging": list(events)}]}


def test_parse_webhook_extracts_audio_attachment():
    p = InstagramProvider()
    url = "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=123&signature=abc"
    incomings = p.parse_webhook(_payload(_audio_event("mid.A1", "CUST1", url)))
    assert len(incomings) == 1
    m = incomings[0]
    assert m.message_type == "audio"
    assert m.audio_url == url
    assert m.from_phone == "ig:CUST1"
    assert m.from_phone.startswith("ig:")
    assert m.provider_message_id == "mid.A1"


def test_parse_webhook_accepts_voice_type():
    """Algunos eventos llegan con type="voice" en vez de "audio"."""
    p = InstagramProvider()
    url = "https://lookaside.fbsbx.com/voice/xyz"
    incomings = p.parse_webhook(_payload(_audio_event("mid.V1", "CUST1", url, att_type="voice")))
    assert len(incomings) == 1
    assert incomings[0].message_type == "audio"
    assert incomings[0].audio_url == url


def test_text_message_still_parses_as_text():
    """No regresión: un mensaje de texto sigue siendo message_type="text"."""
    p = InstagramProvider()
    incomings = p.parse_webhook(_payload(_text_event("mid.T1", "CUST1", "hola")))
    assert len(incomings) == 1
    m = incomings[0]
    assert m.message_type == "text"
    assert m.text == "hola"
    assert m.audio_url is None
    assert m.from_phone == "ig:CUST1"


def test_audio_captured_even_with_text():
    """Si Meta manda texto Y una nota de voz juntos, NO perdemos el audio:
    se capturan ambos (contenido + audio_url) para no descartar la nota."""
    p = InstagramProvider()
    url = "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=999"
    ev = {
        "sender": {"id": "CUST1"},
        "recipient": {"id": "BIZ"},
        "timestamp": 1700000000000,
        "message": {
            "mid": "mid.TA1",
            "text": "te dejo una nota",
            "attachments": [{"type": "audio", "payload": {"url": url}}],
        },
    }
    incomings = p.parse_webhook(_payload(ev))
    assert len(incomings) == 1
    m = incomings[0]
    assert m.message_type == "audio"
    assert m.audio_url == url
    assert m.text == "te dejo una nota"


def test_audio_mime_captured_when_present():
    """Si el adjunto trae mime, lo conservamos (si no, se infiere al descargar)."""
    p = InstagramProvider()
    url = "https://lookaside.fbsbx.com/voice/with-mime"
    ev = {
        "sender": {"id": "CUST1"},
        "recipient": {"id": "BIZ"},
        "timestamp": 1700000000000,
        "message": {
            "mid": "mid.M1",
            "attachments": [
                {"type": "audio", "payload": {"url": url, "mime_type": "audio/mp4"}}
            ],
        },
    }
    incomings = p.parse_webhook(_payload(ev))
    assert len(incomings) == 1
    assert incomings[0].audio_url == url
    assert incomings[0].audio_mime == "audio/mp4"
