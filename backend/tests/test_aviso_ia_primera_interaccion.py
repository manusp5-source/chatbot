"""Aviso determinista de IA en la primera respuesta de canales de texto."""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _Resultado:
    def __init__(self, *, escalar=None, filas=None) -> None:
        self.escalar = escalar
        self.filas = list(filas or [])

    def scalars(self):
        return self

    def all(self):
        return self.filas

    def scalar_one_or_none(self):
        return self.escalar

    def scalar_one(self):
        assert self.escalar is not None
        return self.escalar


class _SesionFalsa:
    def __init__(self, resultados) -> None:
        self.resultados = list(resultados)
        self.consultas = []
        self.anadidos = []

    async def execute(self, consulta):
        self.consultas.append(consulta)
        assert self.resultados, "el drain hizo una consulta de BD no prevista"
        return self.resultados.pop(0)

    def add(self, objeto) -> None:
        self.anadidos.append(objeto)

    async def commit(self) -> None:
        return None


class _ContextoSesion:
    def __init__(self, sesion) -> None:
        self.sesion = sesion

    async def __aenter__(self):
        return self.sesion

    async def __aexit__(self, *_args):
        return False


class _FabricaSesiones:
    def __init__(self, sesiones) -> None:
        self.sesiones = list(sesiones)

    def __call__(self):
        assert self.sesiones, "el drain abrió una sesión de BD no prevista"
        return _ContextoSesion(self.sesiones.pop(0))


def _mensaje(*, conv_id, rol, contenido, extra=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        rol=rol,
        contenido=contenido,
        audio_url=None,
        audio_transcript=None,
        media_type=None,
        extra=extra or {},
        created_at=datetime.now(timezone.utc),
    )


async def _ejecutar_drain(*, canal, respuesta, previos=(), entrenamiento=False):
    """Ejecuta el drain con dobles: interesa el texto que llega al proveedor."""
    from app.models.conversation import ConversationCanal, ConversationStatus
    from app.models.message import MessageRole
    from app.services import conversation as conv_mod
    from app.services import message_buffer

    conv_id = uuid.uuid4()
    contacto_id = uuid.uuid4()
    for previo in previos:
        previo.conversation_id = conv_id
    entrante = _mensaje(
        conv_id=conv_id,
        rol=MessageRole.user,
        contenido="Hola",
    )
    conv = SimpleNamespace(
        id=conv_id,
        contact_id=contacto_id,
        canal=canal,
        status=ConversationStatus.bot,
        spam_reviewed=True,
        last_message_at=None,
    )
    identificador = {
        ConversationCanal.whatsapp: "+34600000000",
        ConversationCanal.instagram_dm: "ig:persona",
        ConversationCanal.web: "web:visitante",
        ConversationCanal.email: "email:cliente@example.com",
    }.get(canal, "voice:llamada")
    contacto = SimpleNamespace(
        id=contacto_id,
        telefono=identificador,
        nombre="Cliente",
        social_handle=None,
    )
    agente = SimpleNamespace(
        id=uuid.uuid4(),
        name="Agente de prueba",
        source="test",
        buffer_seconds=0,
        context_window=20,
        prompt_system="Ayuda al cliente.",
        model_name="modelo-falso",
        temperature=0,
        max_tokens=100,
        tools_enabled=[],
        llm_provider_id=None,
        fallback_provider_id=None,
        fallback_model=None,
        response_split_max_parts=3,
    )

    sesion_carga = _SesionFalsa(
        [
            _Resultado(filas=[entrante]),
            _Resultado(escalar=conv),
            _Resultado(escalar=contacto),
        ]
    )
    sesion_historial = _SesionFalsa([_Resultado(filas=[*previos, entrante])])
    mensaje_real_previo = next(
        (
            mensaje.id
            for mensaje in previos
            if mensaje.rol == MessageRole.assistant
            and not (mensaje.extra or {}).get("is_draft")
        ),
        None,
    )
    resultados_envio = [_Resultado(escalar=conv)]
    if canal in {
        ConversationCanal.whatsapp,
        ConversationCanal.instagram_dm,
        ConversationCanal.web,
    } and not entrenamiento:
        resultados_envio.append(_Resultado(escalar=mensaje_real_previo))
    sesion_envio = _SesionFalsa(resultados_envio)

    entrega = AsyncMock(
        side_effect=lambda *args, **_kwargs: sum(
            bool(parte.strip()) for parte in args[2]
        )
    )
    borrador_email = AsyncMock(
        return_value={"draft_id": "draft-1", "message_id": "gmail-1"}
    )
    fabrica = _FabricaSesiones([sesion_carga, sesion_historial, sesion_envio])

    with ExitStack() as pila:
        pila.enter_context(patch.object(conv_mod, "db_session", fabrica))
        pila.enter_context(
            patch.object(
                conv_mod,
                "get_runtime_for_channel",
                AsyncMock(return_value=agente),
            )
        )
        pila.enter_context(
            patch.object(message_buffer, "should_process", AsyncMock(return_value=True))
        )
        pila.enter_context(
            patch.object(
                message_buffer,
                "drain_queue",
                AsyncMock(return_value=[str(entrante.id)]),
            )
        )
        pila.enter_context(
            patch.object(message_buffer, "drain_peek", AsyncMock(return_value=[]))
        )
        pila.enter_context(
            patch.object(conv_mod, "is_channel_training", AsyncMock(return_value=entrenamiento))
        )
        pila.enter_context(
            patch.object(conv_mod, "_pause_block_reason", AsyncMock(return_value=None))
        )
        pila.enter_context(patch.object(conv_mod, "match_rules", AsyncMock(return_value=None)))
        pila.enter_context(
            patch.object(conv_mod, "email_is_automated", MagicMock(return_value=(False, "")))
        )
        pila.enter_context(
            patch.object(
                conv_mod,
                "moderate",
                AsyncMock(return_value=SimpleNamespace(flagged=False, categories=[])),
            )
        )
        pila.enter_context(patch.object(conv_mod, "can_call_llm", AsyncMock(return_value=True)))
        pila.enter_context(patch.object(conv_mod, "run_agent", AsyncMock(return_value=respuesta)))
        pila.enter_context(patch.object(conv_mod, "deliver_response_parts", entrega))
        pila.enter_context(patch.object(conv_mod, "push_runtime_log", AsyncMock()))
        pila.enter_context(patch.object(conv_mod, "publish_agent_step", AsyncMock()))
        pila.enter_context(patch.object(conv_mod, "log_router_decision", AsyncMock()))
        pila.enter_context(
            patch.object(conv_mod, "event_bus", SimpleNamespace(publish=AsyncMock()))
        )
        pila.enter_context(
            patch(
                "app.services.rolling_summary.maybe_update_rolling_summary",
                AsyncMock(return_value=None),
            )
        )
        pila.enter_context(
            patch(
                "app.services.channel_sender.create_email_draft_for_conversation",
                borrador_email,
            )
        )
        await conv_mod._process_buffered_locked(identificador, [])

    return SimpleNamespace(
        entrega=entrega,
        sesion_envio=sesion_envio,
        borrador_email=borrador_email,
    )


@pytest.mark.asyncio
async def test_la_primera_respuesta_de_whatsapp_empieza_con_el_aviso() -> None:
    from app.models.conversation import ConversationCanal
    from app.services.conversation import AVISO_IA_PRIMERA_INTERACCION

    resultado = await _ejecutar_drain(
        canal=ConversationCanal.whatsapp,
        respuesta="¿En qué te ayudo?",
    )

    partes = resultado.entrega.await_args.args[2]
    assert "\n\n".join(partes).startswith(
        f"{AVISO_IA_PRIMERA_INTERACCION}\n\n¿En qué te ayudo?"
    )


@pytest.mark.asyncio
async def test_la_segunda_respuesta_no_repite_el_aviso() -> None:
    from app.models.conversation import ConversationCanal
    from app.models.message import MessageRole

    conv_id = uuid.uuid4()
    previo = _mensaje(
        conv_id=conv_id,
        rol=MessageRole.assistant,
        contenido="Respuesta anterior",
    )
    resultado = await _ejecutar_drain(
        canal=ConversationCanal.whatsapp,
        respuesta="Seguimos.",
        previos=[previo],
    )

    partes = resultado.entrega.await_args.args[2]
    assert partes == ["Seguimos."]


@pytest.mark.asyncio
async def test_un_borrador_previo_no_cuenta_como_respuesta_del_agente() -> None:
    from app.models.conversation import ConversationCanal
    from app.models.message import MessageRole
    from app.services.conversation import AVISO_IA_PRIMERA_INTERACCION

    previo = _mensaje(
        conv_id=uuid.uuid4(),
        rol=MessageRole.assistant,
        contenido="Sugerencia no enviada",
        extra={"is_draft": True},
    )
    resultado = await _ejecutar_drain(
        canal=ConversationCanal.whatsapp,
        respuesta="Respuesta real",
        previos=[previo],
    )

    partes = resultado.entrega.await_args.args[2]
    assert "\n\n".join(partes).startswith(AVISO_IA_PRIMERA_INTERACCION)
    from sqlalchemy.dialects import postgresql

    consulta = str(
        resultado.sesion_envio.consultas[-1].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert "is_draft" in consulta and "true" in consulta, (
        "la consulta no distingue un borrador de una respuesta entregada"
    )


@pytest.mark.asyncio
async def test_el_correo_no_recibe_el_aviso_automatico() -> None:
    from app.models.conversation import ConversationCanal

    resultado = await _ejecutar_drain(
        canal=ConversationCanal.email,
        respuesta="Borrador para revisar",
    )

    resultado.entrega.assert_not_awaited()
    resultado.borrador_email.assert_awaited_once()
    assert resultado.borrador_email.await_args.args[1] == "Borrador para revisar"


@pytest.mark.asyncio
async def test_el_modo_entrenamiento_no_recibe_el_aviso_automatico() -> None:
    from app.models.conversation import ConversationCanal

    resultado = await _ejecutar_drain(
        canal=ConversationCanal.whatsapp,
        respuesta="Sugerencia para revisar",
        entrenamiento=True,
    )

    resultado.entrega.assert_not_awaited()
    assert [mensaje.contenido for mensaje in resultado.sesion_envio.anadidos] == [
        "Sugerencia para revisar"
    ]


def test_un_texto_vacio_no_se_convierte_en_un_aviso_suelto() -> None:
    from app.services.conversation import aviso_ia_primera_interaccion

    assert aviso_ia_primera_interaccion("", es_primera=True) == ""
    assert aviso_ia_primera_interaccion("   ", es_primera=True) == "   "


def test_un_texto_que_ya_se_presenta_como_asistente_virtual_no_se_duplica() -> None:
    from app.services.conversation import aviso_ia_primera_interaccion

    texto = "Soy el asistente virtual del centro, no una persona. Cuéntame."
    assert aviso_ia_primera_interaccion(texto, es_primera=True) == texto
