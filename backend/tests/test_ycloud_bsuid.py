"""WhatsApp sin teléfono: el cliente con NOMBRE DE USUARIO no se pierde.

Desde abril de 2026, cuando un cliente tiene nombre de usuario de WhatsApp, Meta
OMITE `from`/`wa_id` y solo manda el identificador ámbito-negocio (BSUID). El
parser exigía teléfono y esos mensajes se caían con un `continue` MUDO: no
llegaban a la bandeja, no se creaba contacto, el agente ni se enteraba — y como
el webhook devuelve 200, YCloud lo daba por entregado y no reintentaba. Se
perdían clientes reales.

Cubre:
  - un mensaje que solo trae BSUID entra, con identificador `wa:<bsuid>`;
  - si viene el teléfono, manda el teléfono (el BSUID se REGENERA si el cliente
    cambia de número, así que no es clave estable);
  - lo que sí se descarta (sin identificador, mensajes de sistema) deja TRAZA;
  - los mensajes de sistema de Meta no acaban en la bandeja como si los hubiera
    escrito el cliente;
  - el `@usuario` acaba en `customer_handle` (→ `Contact.social_handle`), el
    campo que ya existía para Instagram;
  - al enviar va `to` O `recipient`, NUNCA los dos (la API los excluye);
  - regresión de TODOS los tipos de contenido que el parser ya soportaba.

Refs:
  https://docs.ycloud.com/reference/whatsapp-inbound-message-webhook-examples
  https://docs.ycloud.com/reference/whatsapp_message-send
  https://developers.facebook.com/docs/whatsapp/business-scoped-user-ids/
"""
from __future__ import annotations

import asyncio

import pytest

from app.providers.whatsapp import ycloud as mod
from app.providers.whatsapp.base import WA_USER_PREFIX
from app.providers.whatsapp.ycloud import YCloudProvider, _recipient_fields

_BSUID = "ES.13491208655302741918"
_PARENT_BSUID = "ES.ENT.11815799212886844830"


def _event(**msg_fields) -> dict:
    """Webhook de YCloud con un único mensaje entrante."""
    msg: dict = {"id": "wamid.M1", "to": "+34900000000", "type": "text"}
    msg.update(msg_fields)
    return {"type": "whatsapp.inbound_message.received", "whatsappInboundMessage": msg}


def _capture_logs(monkeypatch) -> list[dict]:
    """Sustituye `push_runtime_log` por un espía que apunta lo que se registra."""
    registradas: list[dict] = []

    async def _fake(level: str, event: str, message: str | None = None, **fields):
        registradas.append({"level": level, "event": event, "message": message, **fields})

    monkeypatch.setattr(mod, "push_runtime_log", _fake)
    return registradas


# --- Entrada: BSUID como identificador ------------------------------------


def test_solo_bsuid_el_mensaje_entra_con_identificador_wa():
    """El caso que se estaba perdiendo: cliente con nombre de usuario."""
    p = YCloudProvider()
    parsed = p.parse_webhook(
        _event(
            fromUserId=_BSUID,
            fromParentUserId=_PARENT_BSUID,
            text={"body": "Hola, ¿tenéis cita esta semana?"},
        )
    )

    assert len(parsed) == 1
    m = parsed[0]
    assert m.from_phone == f"{WA_USER_PREFIX}{_BSUID}"
    assert m.from_user_id == _BSUID
    assert m.from_parent_user_id == _PARENT_BSUID
    assert m.text == "Hola, ¿tenéis cita esta semana?"


def test_el_bsuid_no_se_trunca():
    """Hasta 128 caracteres más el prefijo de país: va literal."""
    largo = "ES." + "9" * 128
    m = YCloudProvider().parse_webhook(_event(fromUserId=largo, text={"body": "hola"}))[0]
    assert m.from_phone == f"{WA_USER_PREFIX}{largo}"


def test_con_telefono_y_bsuid_manda_el_telefono():
    """El BSUID se regenera al cambiar de número: no es clave inmutable."""
    m = YCloudProvider().parse_webhook(
        _event(**{"from": "34600000000", "fromUserId": _BSUID, "text": {"body": "hola"}})
    )[0]
    assert m.from_phone == "+34600000000"
    # Pero el BSUID se guarda igual: lo necesitan las fases siguientes.
    assert m.from_user_id == _BSUID


async def test_sin_telefono_ni_bsuid_se_descarta_y_queda_traza(monkeypatch):
    """Nunca más un descarte mudo: si se cae algo, tiene que verse en los logs."""
    registradas = _capture_logs(monkeypatch)
    p = YCloudProvider()

    assert p.parse_webhook(_event(text={"body": "sin remitente"})) == []

    await asyncio.sleep(0)  # deja correr la tarea de log
    assert len(registradas) == 1
    log = registradas[0]
    assert log["level"] == "warn"
    assert log["event"] == "ycloud.parse.no_identifier"
    assert log["has_from"] is False and log["has_user_id"] is False
    # Sirve para diagnosticar un cambio de esquema de YCloud…
    assert "msg_keys" in log
    # …pero sin volcar datos personales: solo nombres de campo, no valores.
    assert "sin remitente" not in str(log)


async def test_sin_wamid_tambien_se_descarta_con_traza(monkeypatch):
    """Sin id externo no hay idempotencia posible."""
    registradas = _capture_logs(monkeypatch)
    ev = _event(fromUserId=_BSUID, text={"body": "hola"})
    ev["whatsappInboundMessage"].pop("id")

    assert YCloudProvider().parse_webhook(ev) == []

    await asyncio.sleep(0)
    assert [r["event"] for r in registradas] == ["ycloud.parse.no_identifier"]
    assert registradas[0]["has_wamid"] is False


# --- Mensajes de sistema ---------------------------------------------------


async def test_mensaje_de_sistema_no_llega_a_la_bandeja(monkeypatch):
    """Lo genera Meta, no el cliente. Traía `id` y `from`, así que colaba el
    filtro de arriba y el agente podía acabar contestándole a un aviso."""
    registradas = _capture_logs(monkeypatch)
    ev = _event(
        **{
            "from": "+34600000000",
            "type": "system",
            "system": {
                "body": "User A changed from 123456789 to 987654321",
                "wa_id": "987654321",
                "type": "user_changed_number",
            },
        }
    )

    assert YCloudProvider().parse_webhook(ev) == []

    await asyncio.sleep(0)
    assert [r["event"] for r in registradas] == ["ycloud.parse.system_message_skipped"]
    assert registradas[0]["system_type"] == "user_changed_number"


# --- Nombre de usuario -----------------------------------------------------


def test_el_username_acaba_en_customer_handle():
    """Reutiliza el campo del @usuario de Instagram (→ Contact.social_handle).
    YCloud ya manda la @ incluida: no la tocamos."""
    m = YCloudProvider().parse_webhook(
        _event(
            fromUserId=_BSUID,
            customerProfile={"name": "Joe", "username": "@JoeJoe"},
            text={"body": "hola"},
        )
    )[0]
    assert m.customer_handle == "@JoeJoe"
    assert m.customer_name == "Joe"


def test_sin_username_el_handle_queda_vacio():
    m = YCloudProvider().parse_webhook(
        _event(**{"from": "+34600000000", "customerProfile": {"name": "Ana"},
                  "text": {"body": "hola"}})
    )[0]
    assert m.customer_handle is None
    assert m.customer_name == "Ana"


# --- Regresión: los tipos de contenido que ya funcionaban -------------------


def test_regresion_texto():
    m = YCloudProvider().parse_webhook(
        _event(**{"from": "+34600000000", "text": {"body": "Hola"}})
    )[0]
    assert m.message_type == "text"
    assert m.text == "Hola"
    assert m.media_kind is None


def test_regresion_audio():
    m = YCloudProvider().parse_webhook(
        _event(
            **{
                "from": "+34600000000",
                "type": "audio",
                "audio": {"link": "https://api.ycloud.com/a.ogg", "mime_type": "audio/ogg"},
            }
        )
    )[0]
    assert m.message_type == "audio"
    assert m.audio_url == "https://api.ycloud.com/a.ogg"
    assert m.audio_mime == "audio/ogg"
    # El audio tiene pipeline propio (transcripción): no va por media_*.
    assert m.media_kind is None


def test_regresion_audio_con_url_en_vez_de_link():
    """YCloud manda unas veces `link` y otras `url`."""
    m = YCloudProvider().parse_webhook(
        _event(**{"from": "+34600000000", "type": "audio",
                  "audio": {"url": "https://api.ycloud.com/b.ogg"}})
    )[0]
    assert m.audio_url == "https://api.ycloud.com/b.ogg"


@pytest.mark.parametrize(
    ("mtype", "mime"),
    [
        ("image", "image/jpeg"),
        ("video", "video/mp4"),
        ("document", "application/pdf"),
        ("sticker", "image/webp"),
    ],
)
def test_regresion_adjuntos(mtype: str, mime: str):
    m = YCloudProvider().parse_webhook(
        _event(
            **{
                "from": "+34600000000",
                "type": mtype,
                mtype: {"link": f"https://media.ycloud.com/x.{mtype}", "mime_type": mime},
            }
        )
    )[0]
    assert m.message_type == mtype
    assert m.media_kind == mtype
    assert m.media_url == f"https://media.ycloud.com/x.{mtype}"
    assert m.media_mime == mime


def test_regresion_caption_va_a_texto():
    """El pie de foto lo escribe el cliente: el agente tiene que verlo."""
    m = YCloudProvider().parse_webhook(
        _event(
            **{
                "from": "+34600000000",
                "type": "image",
                "image": {"link": "https://media.ycloud.com/i.jpg", "caption": "el ticket"},
            }
        )
    )[0]
    assert m.text == "el ticket"


def test_regresion_documento_conserva_el_nombre_de_fichero():
    m = YCloudProvider().parse_webhook(
        _event(
            **{
                "from": "+34600000000",
                "type": "document",
                "document": {"link": "https://media.ycloud.com/f.pdf", "filename": "factura.pdf"},
            }
        )
    )[0]
    assert m.media_filename == "factura.pdf"


def test_regresion_adjunto_sin_url_no_inventa_categoria():
    m = YCloudProvider().parse_webhook(
        _event(**{"from": "+34600000000", "type": "image", "image": {"caption": "ups"}})
    )[0]
    assert m.media_kind is None
    assert m.text == "ups"


def test_regresion_evento_que_no_es_entrante_se_ignora():
    p = YCloudProvider()
    assert p.parse_webhook({"type": "whatsapp.outbound_message.delivered"}) == []


def test_regresion_lista_de_eventos_en_un_solo_webhook():
    p = YCloudProvider()
    a = _event(**{"from": "+34600000000", "text": {"body": "uno"}})
    b = _event(fromUserId=_BSUID, text={"body": "dos"})
    b["whatsappInboundMessage"]["id"] = "wamid.M2"
    out = p.parse_webhook([a, b])
    assert [m.provider_message_id for m in out] == ["wamid.M1", "wamid.M2"]
    assert out[1].from_phone == f"{WA_USER_PREFIX}{_BSUID}"


# --- Salida: `to` o `recipient`, nunca los dos -----------------------------


def test_recipient_fields_conmuta_segun_el_identificador():
    assert _recipient_fields(f"{WA_USER_PREFIX}{_BSUID}") == {"recipient": _BSUID}
    assert _recipient_fields(f"{WA_USER_PREFIX}{_PARENT_BSUID}") == {"recipient": _PARENT_BSUID}
    assert _recipient_fields("+34600000000") == {"to": "+34600000000"}
    # Normaliza el teléfono igual que antes.
    assert _recipient_fields("34600000000") == {"to": "+34600000000"}


def test_normalize_phone_no_destroza_un_identificador_wa():
    """Si un `wa:` se cuela por aquí, sale tal cual (antes: `+wa:ES...`)."""
    assert mod._normalize_phone(f"{WA_USER_PREFIX}{_BSUID}") == f"{WA_USER_PREFIX}{_BSUID}"


class _FakeResponse:
    status_code = 200

    @staticmethod
    def json() -> dict:
        return {"id": "wamid.OUT"}

    def raise_for_status(self) -> None:
        return None


class _FakeClient:
    def __init__(self, capturado: dict) -> None:
        self._capturado = capturado

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, _url, json=None, headers=None, files=None):
        self._capturado["json"] = json
        return _FakeResponse()


def _provider_con_credenciales(monkeypatch) -> tuple[YCloudProvider, dict]:
    capturado: dict = {}
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **_kw: _FakeClient(capturado))
    p = YCloudProvider()

    async def _key():
        return "k"

    async def _from():
        return "+34900000000"

    p._api_key = _key  # type: ignore[method-assign]
    p._from_phone = _from  # type: ignore[method-assign]
    return p, capturado


def _asserta_un_solo_destinatario(payload: dict, esperado: dict) -> None:
    """La API exige EXACTAMENTE uno: si van los dos, `to` gana y `recipient` se
    ignora, o sea que el mensaje se iría a un número que no existe."""
    assert ("to" in payload) != ("recipient" in payload)
    for k, v in esperado.items():
        assert payload[k] == v


async def test_send_text_usa_recipient_con_bsuid(monkeypatch):
    p, cap = _provider_con_credenciales(monkeypatch)
    await p.send_text(f"{WA_USER_PREFIX}{_BSUID}", "hola")
    _asserta_un_solo_destinatario(cap["json"], {"recipient": _BSUID})


async def test_send_text_usa_to_con_telefono(monkeypatch):
    p, cap = _provider_con_credenciales(monkeypatch)
    await p.send_text("34600000000", "hola")
    _asserta_un_solo_destinatario(cap["json"], {"to": "+34600000000"})


async def test_send_media_usa_recipient_con_bsuid(monkeypatch):
    p, cap = _provider_con_credenciales(monkeypatch)
    await p.send_media(f"{WA_USER_PREFIX}{_BSUID}", "media-1", "image", caption="mira")
    _asserta_un_solo_destinatario(cap["json"], {"recipient": _BSUID})
    assert cap["json"]["image"]["caption"] == "mira"


async def test_send_media_usa_to_con_telefono(monkeypatch):
    p, cap = _provider_con_credenciales(monkeypatch)
    await p.send_media("+34600000000", "media-1", "image")
    _asserta_un_solo_destinatario(cap["json"], {"to": "+34600000000"})


async def test_send_template_usa_recipient_con_bsuid(monkeypatch):
    p, cap = _provider_con_credenciales(monkeypatch)
    await p.send_template(f"{WA_USER_PREFIX}{_BSUID}", "recordatorio", "es", ["Ana"])
    _asserta_un_solo_destinatario(cap["json"], {"recipient": _BSUID})
    assert cap["json"]["template"]["name"] == "recordatorio"


async def test_send_template_usa_to_con_telefono(monkeypatch):
    p, cap = _provider_con_credenciales(monkeypatch)
    await p.send_template("+34600000000", "recordatorio", "es")
    _asserta_un_solo_destinatario(cap["json"], {"to": "+34600000000"})
