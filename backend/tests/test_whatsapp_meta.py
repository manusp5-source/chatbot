"""WhatsApp por la API Cloud oficial de Meta.

El canal de WhatsApp ya no habla solo con YCloud: cada instalación elige desde
el panel si va por el revendedor o directa a Meta. Este fichero cubre el
proveedor nuevo y, sobre todo, que NO se pierde nada de lo que se arregló
cuando solo existía YCloud:

  - un cliente con nombre de usuario y sin teléfono entra igual, con
    identificador `wa:<bsuid>`, en vez de perderse con un `continue` mudo;
  - enviar sin credenciales LANZA en vez de devolver un centinela que el
    llamante contaría como éxito (el «300 enviados» que no mandó nada);
  - las plantillas montan cabecera y botones, no solo el cuerpo;
  - lo que se descarta deja traza.

Y lo propio de Meta: el saludo inicial del webhook, la firma
`X-Hub-Signature-256` (que es distinta de la de YCloud) y que los adjuntos
lleguen como identificador, no como URL.

Nada de esto se ha podido probar contra la API real de Meta: esta instalación
no tiene credenciales suyas. Lo que se comprueba aquí es lo que construimos
nosotros —el parseo, la firma, el cuerpo de las peticiones y los errores—
contra los ejemplos de su documentación:
  https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/payload-examples
  https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import pytest

from app.providers.whatsapp import meta as mod
from app.providers.whatsapp.base import (
    WA_USER_PREFIX,
    WhatsAppNotConfiguredError,
)
from app.providers.whatsapp.meta import (
    META_MEDIA_PREFIX,
    MetaCloudProvider,
    _recipient_fields,
)

_BSUID = "ES.13491208655302741918"
_PARENT_BSUID = "ES.ENT.11815799212886844830"
_APP_SECRET = "secreto-de-la-app"


def _webhook(mensaje: dict, contacto: dict | None = None) -> dict:
    """Webhook de Meta con un único mensaje entrante.

    Misma envoltura que sus ejemplos: object → entry → changes → value.
    """
    value: dict = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "34900000000", "phone_number_id": "PNID1"},
        "messages": [mensaje],
    }
    if contacto is not None:
        value["contacts"] = [contacto]
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA1", "changes": [{"field": "messages", "value": value}]}],
    }


def _capture_logs(monkeypatch) -> list[dict]:
    """Sustituye `push_runtime_log` por un espía que apunta lo que se registra."""
    registradas: list[dict] = []

    async def _fake(level: str, event: str, message: str | None = None, **fields):
        registradas.append({"level": level, "event": event, "message": message, **fields})

    monkeypatch.setattr(mod, "push_runtime_log", _fake)
    return registradas


def _con_credenciales(monkeypatch, **valores: str) -> None:
    """Finge las credenciales guardadas. Lo que no se pase, no está puesto."""

    async def _fake(key: str, *, strict: bool = False) -> str | None:
        return valores.get(key)

    monkeypatch.setattr(mod, "get_credential", _fake)


# ---------------------------------------------------------------------------
# Entrada: quién escribe
# ---------------------------------------------------------------------------


def test_mensaje_de_texto_normal():
    p = MetaCloudProvider()
    parsed = p.parse_webhook(
        _webhook(
            {"from": "34611223344", "id": "wamid.M1", "type": "text", "text": {"body": "Hola"}},
            {"wa_id": "34611223344", "profile": {"name": "Ana"}},
        )
    )

    assert len(parsed) == 1
    m = parsed[0]
    assert m.provider_message_id == "wamid.M1"
    assert m.from_phone == "+34611223344"
    assert m.to_phone == "+34900000000"
    assert m.message_type == "text"
    assert m.text == "Hola"
    assert m.customer_name == "Ana"


def test_sin_telefono_pero_con_bsuid_el_mensaje_entra():
    """El caso que se perdía enteros: cliente con nombre de usuario.

    Meta deja de mandar el teléfono cuando el cliente tiene nombre de usuario y
    no ha habido contacto reciente. Si exigiéramos teléfono, ese mensaje no
    llegaría a la bandeja y el webhook devolvería 200 tan tranquilo.
    """
    p = MetaCloudProvider()
    # Tal cual lo documenta Meta: el identificador va en el MENSAJE como
    # `from_user_id`, y en el CONTACTO como `user_id`. Son nombres distintos
    # para lo mismo y hay que leer los dos sitios.
    parsed = p.parse_webhook(
        _webhook(
            {
                "from_user_id": _BSUID,
                "from_parent_user_id": _PARENT_BSUID,
                "id": "wamid.M2",
                "type": "text",
                "text": {"body": "¿Tenéis cita?"},
            },
            {
                "user_id": _BSUID,
                "parent_user_id": _PARENT_BSUID,
                "profile": {"name": "Ana", "username": "anita"},
            },
        )
    )

    assert len(parsed) == 1
    m = parsed[0]
    assert m.from_phone == f"{WA_USER_PREFIX}{_BSUID}"
    assert m.from_user_id == _BSUID
    assert m.from_parent_user_id == _PARENT_BSUID
    assert m.text == "¿Tenéis cita?"
    # El @ lo ponemos nosotros: en YCloud viene puesto y aquí no, y la bandeja
    # los tiene que pintar igual.
    assert m.customer_handle == "@anita"


def test_si_viene_telefono_manda_el_telefono():
    """El BSUID se regenera si el cliente cambia de número: no es clave estable."""
    p = MetaCloudProvider()
    parsed = p.parse_webhook(
        _webhook(
            {
                "from": "34611223344",
                "from_user_id": _BSUID,
                "id": "wamid.M3",
                "type": "text",
                "text": {"body": "Hola"},
            },
            {"wa_id": "34611223344", "user_id": _BSUID, "profile": {"name": "Ana"}},
        )
    )
    assert parsed[0].from_phone == "+34611223344"
    # …pero el identificador se conserva igualmente por si hace falta.
    assert parsed[0].from_user_id == _BSUID


def test_sin_ningun_identificador_se_descarta_con_traza(monkeypatch):
    logs = _capture_logs(monkeypatch)
    p = MetaCloudProvider()

    async def _run():
        return p.parse_webhook(_webhook({"id": "wamid.M4", "type": "text", "text": {"body": "x"}}))

    parsed = asyncio.run(_run())
    assert parsed == []
    assert any(x["event"] == "meta_wa.parse.no_identifier" for x in logs)


def test_mensaje_de_sistema_no_llega_a_la_bandeja(monkeypatch):
    """Los genera Meta, no el cliente: si se guardan, el agente le contesta a
    un aviso de Meta con el texto vacío."""
    logs = _capture_logs(monkeypatch)
    p = MetaCloudProvider()

    async def _run():
        return p.parse_webhook(
            _webhook(
                {
                    "from": "34611223344",
                    "id": "wamid.M5",
                    "type": "system",
                    "system": {"type": "user_changed_number", "wa_id": "34611223355"},
                }
            )
        )

    assert asyncio.run(_run()) == []
    assert any(x["event"] == "meta_wa.parse.system_message_skipped" for x in logs)


# ---------------------------------------------------------------------------
# Entrada: qué escribe
# ---------------------------------------------------------------------------


def test_nota_de_voz_llega_como_identificador_de_media():
    """Meta no manda URL: manda un identificador que hay que resolver aparte."""
    p = MetaCloudProvider()
    parsed = p.parse_webhook(
        _webhook(
            {
                "from": "34611223344",
                "id": "wamid.A1",
                "type": "audio",
                "audio": {"id": "MEDIA123", "mime_type": "audio/ogg; codecs=opus", "voice": True},
            }
        )
    )
    m = parsed[0]
    assert m.message_type == "audio"
    assert m.audio_url == f"{META_MEDIA_PREFIX}MEDIA123"
    assert m.audio_mime == "audio/ogg; codecs=opus"


@pytest.mark.parametrize("tipo", ["image", "video", "document", "sticker"])
def test_adjuntos_se_capturan(tipo: str):
    """Sin esto el mensaje se guardaba vacío: una burbuja en blanco en la
    bandeja y el agente sin enterarse de que había un adjunto."""
    p = MetaCloudProvider()
    contenido: dict = {"id": "MEDIA9", "mime_type": "application/pdf"}
    if tipo == "document":
        contenido["filename"] = "factura.pdf"
    if tipo in ("image", "video"):
        contenido["caption"] = "Mira esto"
    parsed = p.parse_webhook(
        _webhook({"from": "34611223344", "id": "wamid.X", "type": tipo, tipo: contenido})
    )
    m = parsed[0]
    assert m.media_kind == tipo
    assert m.media_url == f"{META_MEDIA_PREFIX}MEDIA9"
    if tipo in ("image", "video"):
        # El pie de foto es texto del cliente: va donde el resto del texto.
        assert m.text == "Mira esto"
    if tipo == "document":
        assert m.media_filename == "factura.pdf"


def test_respuesta_a_un_boton_de_plantilla():
    p = MetaCloudProvider()
    parsed = p.parse_webhook(
        _webhook(
            {
                "from": "34611223344",
                "id": "wamid.B1",
                "type": "button",
                "button": {"payload": "SI_CONFIRMO", "text": "Sí, confirmo"},
            }
        )
    )
    # Lo que ve el cliente es el texto del botón: eso es lo que lee el agente.
    assert parsed[0].text == "Sí, confirmo"


def test_respuesta_a_una_lista_o_a_unos_botones():
    p = MetaCloudProvider()
    parsed = p.parse_webhook(
        _webhook(
            {
                "from": "34611223344",
                "id": "wamid.I1",
                "type": "interactive",
                "interactive": {
                    "type": "list_reply",
                    "list_reply": {"id": "op2", "title": "Cita para el martes"},
                },
            }
        )
    )
    assert parsed[0].text == "Cita para el martes"


def test_reaccion_se_conserva():
    """Un 👍 cierra muchas conversaciones: tirarlo sería perder información."""
    p = MetaCloudProvider()
    parsed = p.parse_webhook(
        _webhook(
            {
                "from": "34611223344",
                "id": "wamid.R1",
                "type": "reaction",
                "reaction": {"message_id": "wamid.M1", "emoji": "👍"},
            }
        )
    )
    assert parsed[0].text == "👍"


def test_reaccion_retirada_no_deja_una_burbuja_en_blanco(monkeypatch):
    """Una reacción SIN emoji significa que el cliente la quitó. Guardarla
    dejaría un mensaje vacío en la bandeja y al agente contestando a nada; se
    descarta, pero con traza."""
    logs = _capture_logs(monkeypatch)
    p = MetaCloudProvider()

    async def _run():
        return p.parse_webhook(
            _webhook(
                {
                    "from": "34611223344",
                    "id": "wamid.R2",
                    "type": "reaction",
                    "reaction": {"message_id": "wamid.M1"},
                }
            )
        )

    assert asyncio.run(_run()) == []
    assert any(x["event"] == "meta_wa.parse.empty_content" for x in logs)


def test_el_perfil_se_encuentra_aunque_el_contacto_no_traiga_telefono():
    """Si solo indexáramos los contactos por teléfono, el nombre y el
    `@usuario` se perderían justo en el caso que más importa."""
    p = MetaCloudProvider()
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"display_phone_number": "34900000000"},
                            "contacts": [
                                {"wa_id": "34600000001", "profile": {"name": "Otro"}},
                                {"user_id": _BSUID, "profile": {"name": "Ana", "username": "@anita"}},
                            ],
                            "messages": [
                                {
                                    "from_user_id": _BSUID,
                                    "id": "wamid.M9",
                                    "type": "text",
                                    "text": {"body": "Hola"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    m = p.parse_webhook(payload)[0]
    assert m.customer_name == "Ana"
    # El @ ya venía puesto: no se duplica.
    assert m.customer_handle == "@anita"


def test_varios_bloques_en_el_mismo_aviso():
    """`entry` y `changes` son arrays: Meta puede mandar más de uno y coger
    solo el primero perdería mensajes."""
    p = MetaCloudProvider()
    uno = _webhook(
        {"from": "34611223344", "id": "wamid.A", "type": "text", "text": {"body": "1"}}
    )
    dos = _webhook(
        {"from": "34611223355", "id": "wamid.B", "type": "text", "text": {"body": "2"}}
    )
    juntos = {"object": "whatsapp_business_account", "entry": uno["entry"] + dos["entry"]}
    assert [m.text for m in p.parse_webhook(juntos)] == ["1", "2"]


def test_mensaje_que_whatsapp_no_sabe_entregar_deja_traza(monkeypatch):
    logs = _capture_logs(monkeypatch)
    p = MetaCloudProvider()

    async def _run():
        return p.parse_webhook(
            _webhook(
                {
                    "from": "34611223344",
                    "id": "wamid.U1",
                    "type": "unsupported",
                    "unsupported": {"type": "poll_creation"},
                    "errors": [{"code": 131051, "title": "Message type unknown"}],
                }
            )
        )

    assert asyncio.run(_run()) == []
    assert any(x["event"] == "meta_wa.parse.unsupported_type" for x in logs)


def test_payload_de_otro_producto_se_ignora():
    p = MetaCloudProvider()
    assert p.parse_webhook({"object": "instagram", "entry": []}) == []


# ---------------------------------------------------------------------------
# Estados de entrega
# ---------------------------------------------------------------------------


def test_los_fallos_de_entrega_se_pueden_leer():
    """Un mensaje que Meta rechaza es justo lo que hoy se perdería en silencio."""
    p = MetaCloudProvider()
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "statuses": [
                                {"id": "wamid.S1", "status": "delivered"},
                                {
                                    "id": "wamid.S2",
                                    "status": "failed",
                                    "errors": [{"code": 131047, "title": "Re-engagement message"}],
                                },
                            ]
                        },
                    }
                ],
            }
        ],
    }
    fallidos = [s for s in p.parse_statuses(payload) if s["status"] == "failed"]
    assert len(fallidos) == 1
    assert fallidos[0]["id"] == "wamid.S2"


# ---------------------------------------------------------------------------
# Webhook: saludo inicial y firma
# ---------------------------------------------------------------------------


def test_saludo_inicial_devuelve_el_reto(monkeypatch):
    _con_credenciales(monkeypatch, meta_wa_verify_token="palabra-secreta")
    p = MetaCloudProvider()
    assert (
        asyncio.run(p.verify_webhook_handshake("subscribe", "palabra-secreta", "1234")) == "1234"
    )


def test_saludo_inicial_con_palabra_distinta_se_rechaza(monkeypatch):
    _capture_logs(monkeypatch)
    _con_credenciales(monkeypatch, meta_wa_verify_token="palabra-secreta")
    p = MetaCloudProvider()
    assert asyncio.run(p.verify_webhook_handshake("subscribe", "otra", "1234")) is None


def test_saludo_inicial_sin_palabra_guardada_se_rechaza(monkeypatch):
    _capture_logs(monkeypatch)
    _con_credenciales(monkeypatch)
    p = MetaCloudProvider()
    assert asyncio.run(p.verify_webhook_handshake("subscribe", "loquesea", "1234")) is None


def _firma(cuerpo: bytes, secreto: str = _APP_SECRET) -> str:
    return "sha256=" + hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest()


def test_firma_correcta_pasa(monkeypatch):
    _con_credenciales(monkeypatch, meta_wa_app_secret=_APP_SECRET)
    cuerpo = json.dumps({"hola": "mundo"}).encode()
    p = MetaCloudProvider()
    assert asyncio.run(
        p.verify_webhook_signature({"x-hub-signature-256": _firma(cuerpo)}, cuerpo)
    )


def test_firma_de_otro_secreto_no_pasa(monkeypatch):
    logs = _capture_logs(monkeypatch)
    _con_credenciales(monkeypatch, meta_wa_app_secret=_APP_SECRET)
    cuerpo = b'{"hola":"mundo"}'
    p = MetaCloudProvider()
    assert not asyncio.run(
        p.verify_webhook_signature(
            {"x-hub-signature-256": _firma(cuerpo, "otro-secreto")}, cuerpo
        )
    )
    assert any(x["event"] == "webhook.signature.mismatch" for x in logs)
    # El aviso no puede regalar el secreto: solo su longitud y unas pistas.
    aviso = next(x for x in logs if x["event"] == "webhook.signature.mismatch")
    assert _APP_SECRET not in json.dumps(aviso)


def test_sin_cabecera_de_firma_no_pasa(monkeypatch):
    logs = _capture_logs(monkeypatch)
    _con_credenciales(monkeypatch, meta_wa_app_secret=_APP_SECRET)
    p = MetaCloudProvider()
    assert not asyncio.run(p.verify_webhook_signature({}, b"{}"))
    assert any(x["event"] == "webhook.signature.no_header" for x in logs)


def test_sin_secreto_guardado_no_pasa(monkeypatch):
    """Fail-closed: sin con qué comprobar, no se da por bueno nada."""
    logs = _capture_logs(monkeypatch)
    _con_credenciales(monkeypatch)
    p = MetaCloudProvider()
    assert not asyncio.run(
        p.verify_webhook_signature({"x-hub-signature-256": _firma(b"{}")}, b"{}")
    )
    assert any(x["event"] == "webhook.signature.no_secret" for x in logs)


# ---------------------------------------------------------------------------
# Salida
# ---------------------------------------------------------------------------


def test_destinatario_va_en_to_o_en_recipient_nunca_en_los_dos():
    """El BSUID NO va en `to`: Meta tiene un campo aparte, `recipient`.

    Y si van los dos, Meta da precedencia a `to`, así que mandar ambos con un
    `to` inventado enviaría el mensaje a un número que no existe.
    """
    assert _recipient_fields("+34611223344") == {"to": "34611223344"}
    assert _recipient_fields("34611223344") == {"to": "34611223344"}
    assert _recipient_fields(f"{WA_USER_PREFIX}{_BSUID}") == {"recipient": _BSUID}
    for destino in ("+34611223344", f"{WA_USER_PREFIX}{_BSUID}"):
        campos = _recipient_fields(destino)
        assert len(campos) == 1, campos


def test_enviar_sin_credenciales_lanza_en_vez_de_fingir(monkeypatch):
    """El fallo que contaba 300 envíos sin haber mandado ninguno.

    No puede devolver un identificador falso: quien llama lo persistiría como
    un envío bueno y la bandeja enseñaría la conversación perfectamente
    contestada con el cliente sin recibir nada.
    """
    _con_credenciales(monkeypatch)
    p = MetaCloudProvider()
    with pytest.raises(WhatsAppNotConfiguredError) as exc:
        asyncio.run(p.send_text("+34611223344", "Hola"))
    # Y el motivo tiene que decir QUÉ falta, no un "no configurado" seco.
    assert "identificador del número" in str(exc.value)
    assert "token de acceso" in str(exc.value)


def test_comprobar_credenciales_lanza_si_faltan(monkeypatch):
    """La usa el alta de una campaña ANTES de aceptarla: mejor un error al
    crear que 300 errores uno a uno."""
    _con_credenciales(monkeypatch, meta_wa_phone_number_id="PNID1")
    p = MetaCloudProvider()
    with pytest.raises(WhatsAppNotConfiguredError):
        asyncio.run(p.check_credentials())

    _con_credenciales(monkeypatch, meta_wa_phone_number_id="PNID1", meta_wa_access_token="TK")
    asyncio.run(MetaCloudProvider().check_credentials())  # no lanza


def _captura_envio(monkeypatch) -> list[dict]:
    """Intercepta la llamada a Meta y devuelve los cuerpos que se le mandan."""
    enviados: list[dict] = []

    async def _fake(self, payload: dict, *, idempotency_key: str | None = None) -> str:
        enviados.append(payload)
        return "wamid.OUT"

    monkeypatch.setattr(MetaCloudProvider, "_post_message", _fake)
    return enviados


def test_cuerpo_del_mensaje_de_texto(monkeypatch):
    enviados = _captura_envio(monkeypatch)
    asyncio.run(MetaCloudProvider().send_text("+34611223344", "Hola"))
    assert enviados == [
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": "34611223344",
            "type": "text",
            # En falso a propósito: que un enlace del agente no se convierta en
            # una tarjeta con imagen ajena.
            "text": {"preview_url": False, "body": "Hola"},
        }
    ]


def test_se_puede_escribir_a_quien_no_tiene_telefono(monkeypatch):
    """El arreglo que evitó que se perdieran clientes, ahora también en Meta."""
    enviados = _captura_envio(monkeypatch)
    asyncio.run(MetaCloudProvider().send_text(f"{WA_USER_PREFIX}{_BSUID}", "Hola"))
    assert enviados[0]["recipient"] == _BSUID
    assert "to" not in enviados[0]


def test_adjunto_pone_el_pie_y_el_nombre_solo_donde_toca(monkeypatch):
    enviados = _captura_envio(monkeypatch)
    p = MetaCloudProvider()
    asyncio.run(p.send_media("+34611223344", "MID", "image", caption="Mira"))
    asyncio.run(p.send_media("+34611223344", "MID", "document", filename="f.pdf", caption="Ahí va"))
    asyncio.run(p.send_media("+34611223344", "MID", "audio", caption="se ignora"))

    assert enviados[0]["image"] == {"id": "MID", "caption": "Mira"}
    assert enviados[1]["document"] == {"id": "MID", "filename": "f.pdf", "caption": "Ahí va"}
    # WhatsApp no admite pie en las notas de voz: mandarlo sería un error del
    # proveedor en todos los destinatarios.
    assert enviados[2]["audio"] == {"id": "MID"}


def test_plantilla_monta_cabecera_cuerpo_y_botones(monkeypatch):
    """Antes solo se montaba el cuerpo, así que cualquier plantilla con
    cabecera fallaba en TODOS los destinatarios."""
    enviados = _captura_envio(monkeypatch)
    asyncio.run(
        MetaCloudProvider().send_template(
            "+34611223344",
            "recordatorio",
            "es",
            ["Ana", "martes"],
            header={"format": "image", "media_id": "IMG1"},
            button_variables=[
                {"sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": "abc"}]}
            ],
        )
    )
    plantilla = enviados[0]["template"]
    assert plantilla["name"] == "recordatorio"
    assert plantilla["language"] == {"code": "es"}
    tipos = [c["type"] for c in plantilla["components"]]
    assert tipos == ["header", "body", "button"]
    assert plantilla["components"][0]["parameters"] == [{"type": "image", "image": {"id": "IMG1"}}]
    assert plantilla["components"][1]["parameters"] == [
        {"type": "text", "text": "Ana"},
        {"type": "text", "text": "martes"},
    ]


def test_plantilla_con_variables_con_nombre(monkeypatch):
    """Es lo que genera hoy el asistente de plantillas de Meta ({{nombre}}).
    Sin `parameter_name` Meta rechaza el envío entero."""
    enviados = _captura_envio(monkeypatch)
    asyncio.run(
        MetaCloudProvider().send_template(
            "+34611223344",
            "aviso",
            "es",
            ["Ana"],
            param_format="named",
            variable_names=["nombre"],
        )
    )
    cuerpo = enviados[0]["template"]["components"][0]
    assert cuerpo["parameters"] == [
        {"type": "text", "text": "Ana", "parameter_name": "nombre"}
    ]


def test_cabecera_sin_fichero_avisa_de_lo_que_falta(monkeypatch):
    _captura_envio(monkeypatch)
    with pytest.raises(ValueError) as exc:
        asyncio.run(
            MetaCloudProvider().send_template(
                "+34611223344", "x", "es", header={"format": "image"}
            )
        )
    assert "fichero" in str(exc.value)


# ---------------------------------------------------------------------------
# Plantillas
# ---------------------------------------------------------------------------


def test_listar_plantillas_sin_cuenta_de_negocio_lanza(monkeypatch):
    """Devolver [] mezclaba «no tienes plantillas» con «te falta un dato»."""
    _con_credenciales(monkeypatch, meta_wa_access_token="TK")
    with pytest.raises(WhatsAppNotConfiguredError) as exc:
        asyncio.run(MetaCloudProvider().list_templates())
    assert "cuenta de WhatsApp Business" in str(exc.value)


# ---------------------------------------------------------------------------
# Descarga de adjuntos
# ---------------------------------------------------------------------------


def test_no_se_descarga_de_un_host_cualquiera(monkeypatch):
    """La URL la da Meta, pero se comprueba igual: una respuesta manipulada no
    puede hacer que el backend pida una URL interna con el token puesto."""
    _con_credenciales(monkeypatch, meta_wa_phone_number_id="PNID1", meta_wa_access_token="TK")
    with pytest.raises(ValueError) as exc:
        asyncio.run(
            MetaCloudProvider().download_media("https://evil.example.com/x", max_bytes=1000)
        )
    assert "no permitido" in str(exc.value)


def test_solo_https(monkeypatch):
    _con_credenciales(monkeypatch, meta_wa_phone_number_id="PNID1", meta_wa_access_token="TK")
    with pytest.raises(ValueError):
        asyncio.run(
            MetaCloudProvider().download_media("http://lookaside.fbsbx.com/x", max_bytes=1000)
        )


def test_el_motivo_del_error_de_meta_se_lee():
    """Sin esto, el panel enseña el error genérico de la librería HTTP en vez
    de lo que Meta dice que pasa."""

    class _R:
        text = "{...}"

        def json(self):
            return {
                "error": {
                    "message": "(#131047) Re-engagement message",
                    "code": 131047,
                    "fbtrace_id": "abc",
                }
            }

    detalle = mod._error_detail(_R())  # type: ignore[arg-type]
    assert "Re-engagement" in detalle
    assert "131047" in detalle
