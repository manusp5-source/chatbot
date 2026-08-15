from app.providers.whatsapp.ycloud import YCloudProvider


def test_parse_text_message():
    p = YCloudProvider()
    payload = {
        "type": "whatsapp.inbound_message.received",
        "whatsappInboundMessage": {
            "id": "wamid.ABC",
            "from": "+34600000000",
            "to": "+34900000000",
            "type": "text",
            "text": {"body": "Hola"},
        },
    }
    parsed = p.parse_webhook(payload)
    assert len(parsed) == 1
    m = parsed[0]
    assert m.provider_message_id == "wamid.ABC"
    assert m.from_phone == "+34600000000"
    assert m.message_type == "text"
    assert m.text == "Hola"


def test_parse_audio_message():
    p = YCloudProvider()
    payload = {
        "type": "whatsapp.inbound_message.received",
        "whatsappInboundMessage": {
            "id": "wamid.XYZ",
            "from": "34600000000",
            "to": "+34900000000",
            "type": "audio",
            "audio": {"link": "https://example.com/audio.ogg", "mime_type": "audio/ogg"},
        },
    }
    parsed = p.parse_webhook(payload)
    assert len(parsed) == 1
    m = parsed[0]
    assert m.message_type == "audio"
    assert m.audio_url == "https://example.com/audio.ogg"
    assert m.audio_mime == "audio/ogg"
    # Normaliza con +
    assert m.from_phone.startswith("+")


def test_parse_ignores_unknown_event():
    p = YCloudProvider()
    payload = {"type": "whatsapp.outbound_message.delivered", "whatsappInboundMessage": {}}
    assert p.parse_webhook(payload) == []
