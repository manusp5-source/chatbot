"""Reglas duras del clasificador (filtro sin LLM) y su efecto en Gmail.

Lo que se comprueba:

  R1  Semántica de cada campo: remitente exacto, dominio con subdominios,
      asunto por contenido. Y lo que NO debe cazar.
  R2  Una regla activa retiene el correo SIN llamar al modelo, ni al
      clasificador ni al agente (que en email generaría un borrador).
  R3  Una regla vuelve a retener un hilo YA revisado (es una orden explícita),
      al contrario que el filtro de correo automático.
  R4  La acción sobre Gmail respeta el ajuste: por defecto solo archivan las
      reglas duras, nunca lo que decide la IA.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _db_available() -> bool:
    try:
        import asyncio

        from sqlalchemy import text

        from app.db.session import db_session

        async def _check():
            async with db_session() as db:
                await db.execute(text("SELECT 1"))

        asyncio.run(_check())
        return True
    except Exception:
        return False


pytestmark_db = pytest.mark.skipif(not _db_available(), reason="sin base de datos")


@pytest.fixture(autouse=True)
def _sin_cache_de_reglas():
    """Cada test arranca sin caché de reglas.

    Las reglas viven cacheadas en Redis y solo la tiran el alta, la edición y
    el borrado por la API, que es el único sitio del código que escribe en esa
    tabla. Estos tests las meten a pelo con `db.add`, así que sin esto un test
    vería las reglas del anterior. Es el mismo contrato de producción, puesto
    por escrito: quien escriba reglas por fuera de la API tiene que invalidar.
    """
    import asyncio

    from app.services.classifier_rules import invalidar_cache

    asyncio.run(invalidar_cache())
    yield


def _rule(campo: str, valor: str, canal: str | None = None):
    """Regla suelta, sin base de datos: `rule_matches` no la necesita."""
    return SimpleNamespace(campo=campo, valor=valor, canal=canal, enabled=True)


# --------------------------------------------------------------- R1 (puros)


def test_r1_remitente_es_igualdad_exacta():
    from app.services.classifier_rules import rule_matches

    regla = _rule("remitente", "spam@promo.com")
    assert rule_matches(regla, ["spam@promo.com"], None)
    # Mayúsculas y espacios dan igual: se normaliza al comparar.
    assert rule_matches(regla, ["  SPAM@Promo.com "], None)
    # Pero NO caza por parecido: otro buzón del mismo dominio no está bloqueado.
    assert not rule_matches(regla, ["otro@promo.com"], None)
    assert not rule_matches(regla, ["xspam@promo.com"], None)


def test_r1_dominio_incluye_subdominios_y_no_dominios_parecidos():
    from app.services.classifier_rules import rule_matches

    regla = _rule("dominio", "promo.com")
    assert rule_matches(regla, ["quien.sea@promo.com"], None)
    # De ahí sale el envío masivo: news.promo.com, mail.promo.com...
    assert rule_matches(regla, ["boletin@news.promo.com"], None)
    # "nopromo.com" NO es un subdominio de "promo.com".
    assert not rule_matches(regla, ["hola@nopromo.com"], None)
    assert not rule_matches(regla, ["hola@promo.com.es"], None)


def test_r1_el_dominio_se_puede_escribir_de_tres_formas():
    from app.services.classifier_rules import rule_matches

    for escrito in ("promo.com", "@promo.com", "buzon@promo.com"):
        assert rule_matches(_rule("dominio", escrito), ["x@promo.com"], None), escrito


def test_r1_asunto_es_por_contenido():
    from app.services.classifier_rules import rule_matches

    regla = _rule("asunto", "factura pendiente")
    assert rule_matches(regla, ["ana@cliente.com"], "RE: Factura Pendiente de enero")
    assert not rule_matches(regla, ["ana@cliente.com"], "Consulta de precios")
    # Sin asunto (canales que no son email) no puede cazar.
    assert not rule_matches(regla, ["ana@cliente.com"], None)


def test_r1_el_remitente_vale_para_handle_y_telefono():
    """En Instagram el identificador es el @usuario y en WhatsApp el teléfono:
    la regla mira todas las formas que llegan, no solo la primera."""
    from app.services.classifier_rules import rule_matches

    assert rule_matches(_rule("remitente", "@promos_24"), ["@promos_24", None], None)
    assert rule_matches(_rule("remitente", "+34600111222"), [None, "+34600111222"], None)


def test_r1_una_regla_con_valor_vacio_no_caza_nada():
    """Si no, una fila mal guardada bloquearía el buzón entero."""
    from app.services.classifier_rules import rule_matches

    for campo in ("remitente", "dominio", "asunto"):
        assert not rule_matches(_rule(campo, "   "), ["ana@cliente.com"], "hola")


def test_r1_campo_desconocido_no_caza():
    from app.services.classifier_rules import rule_matches

    assert not rule_matches(_rule("loquesea", "ana@cliente.com"), ["ana@cliente.com"], "x")


# --------------------------------------------------------------- R4 (puros)


@pytest.mark.asyncio
async def test_r4_por_defecto_solo_las_reglas_tocan_gmail():
    from app.services import gmail_quarantine

    with patch.object(
        gmail_quarantine, "get_gmail_action", AsyncMock(return_value="rules")
    ):
        assert await gmail_quarantine._should_touch_gmail("email", by_rule=True)
        # Lo que decide la IA se queda en el panel.
        assert not await gmail_quarantine._should_touch_gmail("email", by_rule=False)


@pytest.mark.asyncio
async def test_r4_el_ajuste_all_incluye_lo_de_la_ia_y_off_no_toca_nada():
    from app.services import gmail_quarantine

    with patch.object(gmail_quarantine, "get_gmail_action", AsyncMock(return_value="all")):
        assert await gmail_quarantine._should_touch_gmail("email", by_rule=False)
    with patch.object(gmail_quarantine, "get_gmail_action", AsyncMock(return_value="off")):
        assert not await gmail_quarantine._should_touch_gmail("email", by_rule=True)


@pytest.mark.asyncio
async def test_r4_los_canales_que_no_son_email_nunca_tocan_gmail():
    from app.services import gmail_quarantine

    with patch.object(gmail_quarantine, "get_gmail_action", AsyncMock(return_value="all")):
        for canal in ("whatsapp", "instagram_dm", "web", "retell_voice"):
            assert not await gmail_quarantine._should_touch_gmail(canal, by_rule=True)


@pytest.mark.asyncio
async def test_r4_un_fallo_de_gmail_no_revienta_la_entrada():
    """Archivar es un extra. Si Gmail falla, el mensaje ya está retenido en el
    panel, que es lo que impide que conteste el bot."""
    from app.services import gmail_quarantine

    with patch.object(
        gmail_quarantine, "_should_touch_gmail", AsyncMock(side_effect=RuntimeError("boom"))
    ):
        assert await gmail_quarantine.archive_in_gmail(uuid.uuid4(), "email", True) is False


# ------------------------------------------------------------ R2/R3/R5 (BD)


async def _correo_entrante(hilo: str, remitente: str, asunto: str, texto: str):
    """Mete un correo por la puerta normal y devuelve (conversación, mensaje)."""
    from app.providers.whatsapp.base import IncomingMessage
    from app.services.conversation import store_incoming

    incoming = IncomingMessage(
        provider_message_id=f"gmail-{uuid.uuid4()}",
        from_phone=f"email:{remitente}",
        to_phone="email:negocio@example.com",
        message_type="text",
        text=texto,
        audio_url=None,
        audio_mime=None,
        customer_name="Quien Sea",
        raw={
            "threadId": hilo,
            "subject": asunto,
            "rfc822_message_id": f"<{hilo}@x>",
            "from_addr": remitente,
            "reply_to_addr": remitente,
            "email_headers": {},
        },
    )
    return await store_incoming(incoming)


@pytestmark_db
@pytest.mark.asyncio
async def test_r2_una_regla_retiene_el_correo_sin_gastar_modelo():
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.classifier_rule import ClassifierRule
    from app.models.conversation import Conversation, ConversationStatus
    from app.services import message_buffer
    from app.services.conversation import process_buffered_messages
    from tests._seed import ensure_text_agent

    await ensure_text_agent()
    dominio = f"bloqueado-{uuid.uuid4().hex[:8]}.com"
    async with db_session() as db:
        db.add(ClassifierRule(campo="dominio", valor=dominio, enabled=True))
        await db.commit()

    hilo = f"hilo-{uuid.uuid4()}"
    msg_id = await _correo_entrante(
        hilo, f"promo@{dominio}", "Oferta irrepetible", "Compra ya"
    )
    assert msg_id is not None

    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.gmail_thread_id == hilo))
        ).scalar_one()
        conv.status = ConversationStatus.bot
        conv_id = conv.id
        await db.commit()

    clave = message_buffer.conversation_key(conv_id)
    await message_buffer.push_message(clave, str(msg_id))

    clasificador = AsyncMock()
    agente = AsyncMock(return_value="no debería llamarse")
    with patch("app.services.conversation.classify_message", clasificador), \
        patch("app.services.conversation.run_agent", agente), \
        patch("app.services.conversation.archive_in_gmail", AsyncMock(return_value=True)) as gmail, \
        patch("app.services.conversation.is_channel_paused", AsyncMock(return_value=False)), \
        patch.object(message_buffer, "should_process", AsyncMock(return_value=True)):
        await process_buffered_messages(clave)

    # Ni clasificador con IA ni agente: la regla decidió antes, coste cero.
    clasificador.assert_not_awaited()
    agente.assert_not_awaited()
    # Y sí se pidió archivar en Gmail, marcado como "viene de una regla".
    assert gmail.await_count == 1
    assert gmail.await_args.kwargs["by_rule"] is True

    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.id == conv_id))
        ).scalar_one()
        assert conv.quarantined_at is not None
        assert dominio in (conv.quarantine_reason or "")
        # El motivo dice que fue una regla, no "spam" a secas.
        assert "dominio bloqueado" in (conv.quarantine_reason or "")

    # El contador de la regla sube: sin él no hay forma de saber si sobra.
    async with db_session() as db:
        regla = (
            await db.execute(select(ClassifierRule).where(ClassifierRule.valor == dominio))
        ).scalar_one()
        assert regla.hits == 1
        assert regla.last_hit_at is not None


@pytestmark_db
@pytest.mark.asyncio
async def test_r3_la_regla_vuelve_a_retener_un_hilo_ya_revisado():
    """Al revés que el filtro de correo automático: si alguien liberó el hilo y
    DESPUÉS se escribió la regla, manda la regla."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.classifier_rule import ClassifierRule
    from app.models.conversation import Conversation, ConversationStatus
    from app.services import message_buffer
    from app.services.conversation import process_buffered_messages
    from tests._seed import ensure_text_agent

    await ensure_text_agent()
    remitente = f"pesado-{uuid.uuid4().hex[:8]}@example.com"
    async with db_session() as db:
        db.add(ClassifierRule(campo="remitente", valor=remitente, enabled=True))
        await db.commit()

    hilo = f"hilo-{uuid.uuid4()}"
    msg_id = await _correo_entrante(hilo, remitente, "Otra vez", "Hola")
    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.gmail_thread_id == hilo))
        ).scalar_one()
        conv.status = ConversationStatus.bot
        conv.spam_reviewed = True  # ya se revisó y se liberó en su día
        conv_id = conv.id
        await db.commit()

    clave = message_buffer.conversation_key(conv_id)
    await message_buffer.push_message(clave, str(msg_id))
    with patch("app.services.conversation.classify_message", AsyncMock()) as clasificador, \
        patch("app.services.conversation.run_agent", AsyncMock()) as agente, \
        patch("app.services.conversation.archive_in_gmail", AsyncMock(return_value=True)), \
        patch("app.services.conversation.is_channel_paused", AsyncMock(return_value=False)), \
        patch.object(message_buffer, "should_process", AsyncMock(return_value=True)):
        await process_buffered_messages(clave)

    clasificador.assert_not_awaited()
    agente.assert_not_awaited()
    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.id == conv_id))
        ).scalar_one()
        assert conv.quarantined_at is not None


@pytestmark_db
@pytest.mark.asyncio
async def test_r2_una_regla_desactivada_no_filtra():
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.classifier_rule import ClassifierRule
    from app.services.classifier_rules import invalidar_cache, match_rules

    valor = f"apagada-{uuid.uuid4().hex[:8]}@example.com"
    async with db_session() as db:
        db.add(ClassifierRule(campo="remitente", valor=valor, enabled=False))
        await db.commit()
    assert await match_rules([valor], None, "email") is None

    async with db_session() as db:
        regla = (
            await db.execute(select(ClassifierRule).where(ClassifierRule.valor == valor))
        ).scalar_one()
        regla.enabled = True
        await db.commit()
    # Escrita a pelo, sin pasar por la API: hay que tirar la caché a mano (lo
    # que hace el endpoint por su cuenta).
    await invalidar_cache()
    hit = await match_rules([valor], None, "email")
    assert hit is not None and hit.campo == "remitente"


@pytestmark_db
@pytest.mark.asyncio
async def test_r2_una_regla_de_otro_canal_no_aplica():
    from app.db.session import db_session
    from app.models.classifier_rule import ClassifierRule
    from app.services.classifier_rules import match_rules

    valor = f"solo-ig-{uuid.uuid4().hex[:8]}"
    async with db_session() as db:
        db.add(ClassifierRule(campo="remitente", valor=valor, canal="instagram_dm", enabled=True))
        await db.commit()
    assert await match_rules([valor], None, "email") is None
    assert await match_rules([valor], None, "instagram_dm") is not None


# ---------------------------------------------- Arreglos de la auditoría (A1-A6)
#
# A1  Una etiqueta de sistema de Gmail ("Spam", "Trash") no se puede usar para
#     archivar: marcaría el correo como spam o lo tiraría a la papelera.
# A2  Una regla no puede ser un dominio de un solo nivel ni un asunto de dos
#     letras: barrería el buzón entero en silencio.
# A3  Archivar toca SOLO los mensajes de la tanda, no el hilo entero.
# A4  Devolver a Recibidos toca SOLO lo que archivamos nosotros, con la etiqueta
#     con la que se archivó.
# A5  Liberar una conversación que una regla volvería a cazar se rechaza con un
#     mensaje que dice qué regla es.
# A6  Editar una regla valida igual que crearla.


def test_a1_ensure_label_nunca_devuelve_una_de_sistema():
    from app.providers.gmail.client import GmailClient

    labels = [
        {"id": "SPAM", "name": "SPAM", "type": "system"},
        {"id": "TRASH", "name": "TRASH", "type": "system"},
        {"id": "Label_9", "name": "Descartado por el bot", "type": "user"},
    ]
    # "Spam" escrito en el panel NO puede resolver a la etiqueta de sistema.
    assert GmailClient._find_user_label(labels, "Spam") is None
    assert GmailClient._find_user_label(labels, "trash") is None
    # La propia sí, sin distinguir mayúsculas.
    assert GmailClient._find_user_label(labels, "descartado por el bot") == "Label_9"
    # Sin `type` se asume propia (Gmail siempre lo manda; esto es por si acaso).
    assert GmailClient._find_user_label([{"id": "L1", "name": "Otra"}], "otra") == "L1"


@pytest.mark.asyncio
async def test_a1_archivar_no_usa_un_id_de_etiqueta_de_sistema():
    """Segundo cerrojo: aunque llegara un id de sistema, no se archiva."""
    from app.services import gmail_quarantine

    gmail = AsyncMock()
    gmail.ensure_label = AsyncMock(return_value="SPAM")
    gmail.modify_message = AsyncMock(return_value=True)
    with patch.object(gmail_quarantine, "_should_touch_gmail", AsyncMock(return_value=True)), \
        patch.object(
            gmail_quarantine, "get_label_name", AsyncMock(return_value="Descartado por el bot")
        ), \
        patch.object(
            gmail_quarantine, "_mensajes_a_archivar", AsyncMock(return_value=[(uuid.uuid4(), "m1")])
        ), \
        patch("app.providers.gmail.get_gmail_provider", return_value=gmail):
        assert await gmail_quarantine.archive_in_gmail(
            uuid.uuid4(), "email", True, [uuid.uuid4()]
        ) is False
    gmail.modify_message.assert_not_awaited()


def test_a2_valores_que_barrerian_el_buzon_se_rechazan():
    from fastapi import HTTPException

    from app.api.admin import _valida_valor

    # Un dominio de un solo nivel caza a todo el mundo (la comparación acepta
    # subdominios, así que "com" es todo internet).
    for malo in ("com", ".es", "@com"):
        with pytest.raises(HTTPException) as exc:
            _valida_valor("dominio", malo)
        assert exc.value.status_code == 422
    # Un asunto de dos letras está dentro de casi cualquier correo.
    with pytest.raises(HTTPException):
        _valida_valor("asunto", "of")
    with pytest.raises(HTTPException):
        _valida_valor("remitente", "   ")
    # Y lo razonable pasa, ya normalizado.
    assert _valida_valor("dominio", "  Buzon@Agencia.COM ") == "agencia.com"
    assert _valida_valor("asunto", " Oferta Irrepetible ") == "oferta irrepetible"


@pytest.mark.asyncio
async def test_a3_archivar_solo_toca_los_mensajes_de_la_tanda():
    """En email la conversación ES el hilo: sacar de Recibidos los ocho correos
    anteriores por culpa del noveno era el fallo."""
    from app.services import gmail_quarantine

    de_la_tanda = uuid.uuid4()
    gmail = AsyncMock()
    gmail.ensure_label = AsyncMock(return_value="Label_9")
    gmail.modify_message = AsyncMock(return_value=True)
    marcados: list = []
    with patch.object(gmail_quarantine, "_should_touch_gmail", AsyncMock(return_value=True)), \
        patch.object(
            gmail_quarantine, "get_label_name", AsyncMock(return_value="Descartado por el bot")
        ), \
        patch.object(
            gmail_quarantine,
            "_mensajes_a_archivar",
            AsyncMock(return_value=[(de_la_tanda, "gmail-9")]),
        ) as buscar, \
        patch.object(
            gmail_quarantine, "_marcar", AsyncMock(side_effect=lambda m, l: marcados.append((m, l)))
        ), \
        patch("app.providers.gmail.get_gmail_provider", return_value=gmail):
        ok = await gmail_quarantine.archive_in_gmail(
            uuid.uuid4(), "email", True, [de_la_tanda]
        )
    assert ok is True
    # Se pidieron SOLO los mensajes de la tanda.
    assert buscar.await_args.args[0] == [de_la_tanda]
    gmail.modify_message.assert_awaited_once_with("gmail-9", add=["Label_9"], remove=["INBOX"])
    # Y queda anotado con qué etiqueta se archivó, para poder deshacerlo luego.
    assert marcados == [(de_la_tanda, "Label_9")]


@pytest.mark.asyncio
async def test_a4_devolver_a_recibidos_solo_toca_lo_que_archivamos_nosotros():
    """Antes metía INBOX en todo el hilo: eso resucitaba correos que ella había
    archivado a mano hace meses."""
    from app.services import gmail_quarantine

    msg_id = uuid.uuid4()
    gmail = AsyncMock()
    gmail.modify_message = AsyncMock(return_value=True)
    gmail.ensure_label = AsyncMock(return_value="Label_NUEVA")
    desmarcados: list = []
    with patch.object(
        gmail_quarantine,
        "_mensajes_archivados",
        AsyncMock(return_value=[(msg_id, "gmail-9", "Label_VIEJA")]),
    ), patch.object(
        gmail_quarantine, "_marcar", AsyncMock(side_effect=lambda m, l: desmarcados.append((m, l)))
    ), patch("app.providers.gmail.get_gmail_provider", return_value=gmail):
        assert await gmail_quarantine.restore_in_gmail(uuid.uuid4(), "email") is True

    # Quita la etiqueta CON LA QUE SE ARCHIVÓ, no la que esté configurada hoy.
    gmail.modify_message.assert_awaited_once_with(
        "gmail-9", add=["INBOX"], remove=["Label_VIEJA"]
    )
    assert desmarcados == [(msg_id, None)]


@pytest.mark.asyncio
async def test_a4_sin_nada_archivado_no_se_toca_gmail_al_liberar():
    from app.services import gmail_quarantine

    gmail = AsyncMock()
    with patch.object(
        gmail_quarantine, "_mensajes_archivados", AsyncMock(return_value=[])
    ), patch("app.providers.gmail.get_gmail_provider", return_value=gmail):
        assert await gmail_quarantine.restore_in_gmail(uuid.uuid4(), "email") is False
    gmail.modify_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a3_el_correo_nuevo_de_un_hilo_ya_archivado_se_archiva_tambien():
    from app.services import gmail_quarantine

    gmail = AsyncMock()
    gmail.ensure_label = AsyncMock(return_value="Label_9")
    gmail.modify_message = AsyncMock(return_value=True)
    nuevo = uuid.uuid4()
    with patch.object(
        gmail_quarantine,
        "_mensajes_archivados",
        AsyncMock(return_value=[(uuid.uuid4(), "gmail-1", "Label_9")]),
    ), patch.object(
        gmail_quarantine, "_mensajes_a_archivar", AsyncMock(return_value=[(nuevo, "gmail-2")])
    ), patch.object(
        gmail_quarantine, "get_label_name", AsyncMock(return_value="Descartado por el bot")
    ), patch.object(gmail_quarantine, "_marcar", AsyncMock()), \
        patch("app.providers.gmail.get_gmail_provider", return_value=gmail):
        assert await gmail_quarantine.archive_followup_in_gmail(
            uuid.uuid4(), "email", nuevo
        ) is True
    gmail.modify_message.assert_awaited_once_with("gmail-2", add=["Label_9"], remove=["INBOX"])


@pytest.mark.asyncio
async def test_a3_si_al_hilo_no_le_archivamos_nada_el_correo_nuevo_tampoco():
    """El hilo lo retuvo la IA y el ajuste dice que la IA no toca el buzón: el
    correo siguiente tampoco se archiva."""
    from app.services import gmail_quarantine

    gmail = AsyncMock()
    with patch.object(
        gmail_quarantine, "_mensajes_archivados", AsyncMock(return_value=[])
    ), patch("app.providers.gmail.get_gmail_provider", return_value=gmail):
        assert await gmail_quarantine.archive_followup_in_gmail(
            uuid.uuid4(), "email", uuid.uuid4()
        ) is False
    gmail.modify_message.assert_not_awaited()


@pytestmark_db
@pytest.mark.asyncio
async def test_a5_no_se_puede_liberar_lo_que_una_regla_volveria_a_cazar():
    """Liberar y que la regla lo vuelva a coger a los dos segundos es peor que
    no dejar liberar: el operador no entiende por qué vuelve a Descartados."""
    from datetime import datetime, timezone

    from fastapi import HTTPException
    from sqlalchemy import select

    from app.api.conversations import release_quarantine
    from app.db.session import db_session
    from app.models.classifier_rule import ClassifierRule
    from app.models.conversation import Conversation, ConversationStatus
    from app.models.user import User

    remitente = f"pesado-{uuid.uuid4().hex[:8]}@example.com"
    hilo = f"hilo-{uuid.uuid4()}"
    msg_id = await _correo_entrante(hilo, remitente, "Otra vez", "Hola")
    assert msg_id is not None

    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.gmail_thread_id == hilo))
        ).scalar_one()
        conv.status = ConversationStatus.bot
        conv.quarantined_at = datetime.now(timezone.utc)
        conv.quarantine_reason = "remitente bloqueado"
        conv_id = conv.id
        db.add(ClassifierRule(campo="remitente", valor=remitente, enabled=True))
        await db.commit()

    async with db_session() as db:
        admin = User(
            email=f"admin-regla-{uuid.uuid4().hex[:8]}@test.local",
            password_hash="x",
            nombre="Admin de prueba",
            role="admin",
        )
        db.add(admin)
        await db.commit()
        await db.refresh(admin)
    async with db_session() as db:
        with pytest.raises(HTTPException) as exc:
            await release_quarantine(conv_id, db=db, current_user=admin)
    assert exc.value.status_code == 409
    # El mensaje dice QUÉ regla, si no hay que adivinarlo.
    assert remitente in str(exc.value.detail)

    # Desactivada la regla, ya se puede liberar.
    from app.services.classifier_rules import invalidar_cache

    async with db_session() as db:
        regla = (
            await db.execute(select(ClassifierRule).where(ClassifierRule.valor == remitente))
        ).scalar_one()
        regla.enabled = False
        await db.commit()
    # Escrita a pelo, sin pasar por la API: hay que tirar la caché a mano (es
    # lo que hace el endpoint por su cuenta).
    await invalidar_cache()
    async with db_session() as db:
        with patch(
            "app.services.gmail_quarantine.restore_in_gmail", AsyncMock(return_value=False)
        ), patch("app.tasks.process_message.process_message.delay", lambda *a, **k: None):
            await release_quarantine(conv_id, db=db, current_user=admin)
    async with db_session() as db:
        conv = (
            await db.execute(select(Conversation).where(Conversation.id == conv_id))
        ).scalar_one()
        assert conv.quarantined_at is None
        assert conv.spam_reviewed is True


# ------------------------------------------------------------------- Caché
#
# Las reglas se consultaban en la base EN CADA MENSAJE ENTRANTE, siendo un
# puñado de filas que cambian una vez al mes. Ahora van en Redis. Lo que se
# comprueba aquí es que la caché no puede hacer daño: ni filtrar de más, ni
# dejar una regla desactivada haciendo estragos mientras alguien mira el buzón.


@pytestmark_db
@pytest.mark.asyncio
async def test_cache_la_segunda_vez_no_toca_la_base():
    from app.db.session import db_session
    from app.models.classifier_rule import ClassifierRule
    from app.services import classifier_rules

    valor = f"cacheado-{uuid.uuid4().hex[:8]}@example.com"
    async with db_session() as db:
        db.add(ClassifierRule(campo="remitente", valor=valor, enabled=True))
        await db.commit()

    assert (await classifier_rules.match_rules([valor], None, "email")) is not None

    with patch.object(
        classifier_rules, "_leer_de_la_base", AsyncMock(side_effect=AssertionError("fue a la base"))
    ) as base:
        hit = await classifier_rules.match_rules([valor], None, "email")
    base.assert_not_awaited()
    assert hit is not None and hit.valor == valor


@pytestmark_db
@pytest.mark.asyncio
async def test_cache_una_instalacion_sin_reglas_tampoco_va_a_la_base_cada_vez():
    """"No hay caché" y "no hay reglas" son cosas distintas: sin distinguirlas,
    la instalación más común (ninguna regla) consultaba en cada mensaje."""
    from app.services import classifier_rules

    await classifier_rules.invalidar_cache()
    with patch.object(classifier_rules, "_leer_de_la_base", AsyncMock(return_value=[])) as base:
        await classifier_rules.reglas_activas()
        await classifier_rules.reglas_activas()
    assert base.await_count == 1


@pytestmark_db
@pytest.mark.asyncio
async def test_dar_de_alta_una_regla_por_la_api_tira_la_cache():
    """Sin esto, una regla nueva tardaría hasta cinco minutos en aplicarse."""
    import uuid as _uuid

    from app.api.admin import create_classifier_rule
    from app.api.admin import ClassifierRuleIn
    from app.db.session import db_session
    from app.models.user import User
    from app.services import classifier_rules

    valor = f"nueva-{_uuid.uuid4().hex[:8]}@example.com"
    # Calienta la caché SIN esa regla dentro.
    await classifier_rules.invalidar_cache()
    assert await classifier_rules.match_rules([valor], None, "email") is None

    async with db_session() as db:
        admin = User(
            email=f"admin-cache-{_uuid.uuid4().hex[:8]}@test.local",
            password_hash="x",
            nombre="Admin de prueba",
            role="admin",
        )
        db.add(admin)
        await db.commit()
        await db.refresh(admin)
    async with db_session() as db:
        await create_classifier_rule(
            ClassifierRuleIn(campo="remitente", valor=valor), db=db, me=admin
        )

    assert await classifier_rules.match_rules([valor], None, "email") is not None, (
        "la regla recién creada no se aplicaba hasta que caducara la caché"
    )


@pytest.mark.asyncio
async def test_sin_redis_se_lee_de_la_base_y_no_se_filtra_de_menos():
    """Una caché caída no puede dejar pasar lo que hay que retener."""
    from app.services import classifier_rules

    regla = classifier_rules._Regla(
        id=uuid.uuid4(), campo="remitente", valor="spam@promo.com", canal=None
    )

    def _redis_muerto():
        raise RuntimeError("Redis no responde")

    with patch.object(classifier_rules, "get_redis", _redis_muerto), patch.object(
        classifier_rules, "_leer_de_la_base", AsyncMock(return_value=[regla])
    ):
        hit = await classifier_rules.match_rules(["spam@promo.com"], None, "email")
    assert hit is not None and hit.campo == "remitente"


@pytest.mark.asyncio
async def test_una_cache_rota_se_ignora_en_vez_de_reventar():
    from app.services import classifier_rules

    class _RedisConBasura:
        async def get(self, _k):
            return "{esto no es json"

        async def set(self, *a, **k):
            return True

    with patch.object(classifier_rules, "get_redis", lambda: _RedisConBasura()), patch.object(
        classifier_rules, "_leer_de_la_base", AsyncMock(return_value=[])
    ) as base:
        assert await classifier_rules.reglas_activas() == []
    base.assert_awaited_once()
