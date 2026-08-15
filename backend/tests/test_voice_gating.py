"""El canal de VOZ tiene que obedecer los mismos frenos que los de texto.

Lo que cubre este fichero (auditoría del canal Retell):

  B1 — El botón "Pausado" no hacía NADA en voz. Y como el tope mensual de gasto
       se aplica llamando a `set_agent_paused(True)` (pausa GLOBAL), al pasarse
       de presupuesto se apagaban WhatsApp, web, Instagram y email y el único
       que seguía gastando era el canal de llamadas, que es el más caro (tokens
       + minutos de Retell).
  B2 — Tampoco miraba moderación, ni si un operador había tomado la
       conversación desde la bandeja, ni la cuarentena. Y buscaba la
       conversación sin filtrar canal ni llamada.
  B5 — El WebSocket no tenía tope de intentos: cada conexión ilegítima cuesta
       peticiones a la API de Retell del cliente más un segundo de espera.
  I1 — Un silencio (`reminder_required`) se procesaba como turno nuevo: se
       repetía la respuesta anterior palabra por palabra y se pagaba otra vez.
  I2 — El historial iba sin ventana (coste al cuadrado con la duración).
  I3 — Quien llamaba se identificaba por id de llamada, no por su teléfono.
  I8 — El agente no podía colgar nunca.
  I9 — El bucle del WebSocket se bloqueaba esperando al modelo y los pings de
       Retell salían tarde → "conexión perdida".

Los casos que tocan BD van con el mismo skipif que `test_voice_agent.py`.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest


# ---------------------------------------------------------------------------
# DB-gate (mismo patrón que test_voice_agent.py)
# ---------------------------------------------------------------------------


def _db_available() -> bool:
    try:
        from sqlalchemy import text

        from app.db.session import db_session

        async def _check():
            async with db_session() as db:
                await db.execute(text("select 1"))

        asyncio.run(_check())
        return True
    except Exception:
        return False


pytestmark_db = pytest.mark.skipif(
    not _db_available(),
    reason="DB no disponible en este entorno (se ejecuta en CI con Postgres)",
)


# ---------------------------------------------------------------------------
# Dobles
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict = {}
        self.expires: dict = {}

    async def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    async def expire(self, key, seconds):
        self.expires[key] = seconds
        return True


def _runtime(**over):
    """AgentRuntime mínimo para los tests."""
    from app.services.runtime_config import AgentRuntime

    defaults = dict(
        id=uuid.uuid4(),
        name="Agente de Voz",
        prompt_system="Eres el agente.",
        model_name="gpt-test",
        temperature=1.0,
        max_tokens=0,
        buffer_seconds=1,
        response_split_max_parts=1,
        context_window=4,
        handoff_bridge_message=None,
        monthly_budget_usd=None,
        tools_enabled=["consultar_kb"],
        source="agents",
    )
    defaults.update(over)
    return AgentRuntime(**defaults)


def _silence_side_effects(monkeypatch):
    """Apaga runtime logs / alertas / textos configurables (Redis y BD fuera)."""
    from app.api import voice

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(voice, "push_runtime_log", _noop)
    monkeypatch.setattr(voice, "notify_security", _noop)
    monkeypatch.setattr(voice, "get_channel_config", _noop)  # → textos por defecto


# ---------------------------------------------------------------------------
# Lógica pura (sin DB)
# ---------------------------------------------------------------------------


def test_latest_user_turn_devuelve_indice():
    """Menor: el historial se cortaba con `transcript[:-1]` mientras el texto se
    buscaba hacia atrás. Si el último turno NO era del usuario, ese mismo turno
    entraba dos veces (en el historial y como mensaje actual)."""
    from app.api.voice import _latest_user_turn

    transcript = [
        {"role": "agent", "content": "Hola"},
        {"role": "user", "content": "Quiero cita"},
        {"role": "agent", "content": "Claro"},  # el último NO es del usuario
    ]
    text, idx = _latest_user_turn(transcript)
    assert text == "Quiero cita"
    assert idx == 1  # con `[:-1]` el turno del usuario habría entrado también


def test_latest_user_turn_vacio():
    from app.api.voice import _latest_user_turn

    assert _latest_user_turn([]) == ("", -1)
    assert _latest_user_turn([{"role": "agent", "content": "Hola"}]) == ("", -1)


def test_history_recorta_a_la_ventana():
    """I2: el transcript entero ya no se reenvía en cada turno."""
    from app.api.voice import _build_history

    transcript = [{"role": "user", "content": f"m{i}"} for i in range(50)]
    transcript.append({"role": "user", "content": "el actual"})
    history = asyncio.run(_build_history(transcript, upto=50, context_window=6))
    assert len(history) == 6
    assert history[-1].content == "m49"
    assert all(h.content != "el actual" for h in history)


def test_history_excluye_el_turno_actual():
    from app.api.voice import _build_history

    transcript = [
        {"role": "agent", "content": "Hola"},
        {"role": "user", "content": "Quiero cita"},
        {"role": "agent", "content": "Claro"},
    ]
    history = asyncio.run(_build_history(transcript, upto=1, context_window=20))
    assert [h.content for h in history] == ["Hola"]
    assert history[0].role == "assistant"


def test_marca_de_fin_de_llamada():
    """I8: el agente ya tiene una forma de colgar, y la marca no se dice."""
    from app.api.voice import END_CALL_MARKER, _strip_end_call_marker

    r = _strip_end_call_marker(f"Gracias por llamar. {END_CALL_MARKER}")
    assert r.end_call is True
    assert END_CALL_MARKER not in r.text
    assert r.text == "Gracias por llamar."

    r2 = _strip_end_call_marker("¿Algo más?")
    assert r2.end_call is False
    assert r2.text == "¿Algo más?"


def test_prompt_de_voz_explica_el_recado():
    """La voz no tiene `derivar_humano` a propósito; que al menos pueda tomar el
    recado y colgar en vez de quedarse sin salida."""
    from app.api.voice import VOICE_END_CALL_INSTRUCTIONS

    low = VOICE_END_CALL_INSTRUCTIONS.lower()
    assert "recado" in low
    assert "no puedes pasar la llamada" in low


def test_from_number_se_normaliza():
    """I3: el teléfono de quien llama ya venía en `call_details` y se tiraba."""
    from app.api.voice import _extract_from_number, _normalize_from_number

    assert _normalize_from_number("+34600111222") == "+34600111222"
    assert _normalize_from_number("+34 600 111 222") == "+34600111222"
    # Llamadas sin identificación → None (se usa la clave sintética).
    assert _normalize_from_number("anonymous") is None
    assert _normalize_from_number("") is None
    assert _normalize_from_number(None) is None
    assert _normalize_from_number("no-es-un-telefono") is None

    payload = {
        "interaction_type": "call_details",
        "call": {"call_id": "call_1", "from_number": "+34600111222"},
    }
    assert _extract_from_number(payload) == "+34600111222"
    assert _extract_from_number({"interaction_type": "call_details", "call": {}}) is None


def test_coste_de_telefonia_de_retell():
    """I10: Retell manda el coste de los minutos en centavos."""
    from app.api.voice import _call_cost_usd

    assert _call_cost_usd({"call_cost": {"combined_cost": 250}}) == 2.5
    assert _call_cost_usd({}) is None
    assert _call_cost_usd({"call_cost": {}}) is None


def test_endpoint_legacy_retirado():
    """El HTTP `/voice/retell/llm` decía en su propio docstring que Retell no lo
    usa y su esquema de firma ya no correspondía al actual."""
    from app.main import app

    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/api/v1/voice/retell/llm" not in paths
    assert "/api/v1/voice/retell/llm-ws/{call_id}" in paths
    assert "/api/v1/voice/retell/webhook" in paths
    assert "/api/v1/voice/status" in paths


def test_provision_devuelve_las_dos_urls():
    """B3: el modal enseñaba UNA de las dos URLs y daba a entender que ya estaba
    todo. Sin la del webhook, cada llamada se queda sin duración, sin grabación,
    sin resumen y sin motivo de fin."""
    from app.api.admin import RetellProvisionOut

    campos = set(RetellProvisionOut.model_fields)
    assert {"llm_webhook_url", "call_webhook_url"} <= campos
    assert "signed_recordings_enabled" in campos


def test_textos_del_canal_son_configurables():
    """Menor: "Sigo aquí, dime." era el único castellano del canal que no se
    podía cambiar desde el panel."""
    from app.api.voice import VOICE_TEXTS

    assert {
        "greeting",
        "reminder_message",
        "unavailable_message",
        "handoff_message",
        "training_message",
        "error_message",
    } <= set(VOICE_TEXTS)


def test_ws_limita_intentos_por_ip(monkeypatch):
    """B5: sin tope, cada conexión basura quema cuota de la API de Retell del
    cliente y retiene la conexión un segundo. Amplificación barata."""
    from app.core.config import settings
    from app.providers.voice import retell as retell_mod

    fake = _FakeRedis()
    monkeypatch.setattr(retell_mod, "get_redis", lambda: fake)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(retell_mod, "push_runtime_log", _noop)
    monkeypatch.setattr(settings, "RETELL_WS_MAX_ATTEMPTS_PER_MIN", 3)

    async def _run():
        return [await retell_mod.register_ws_attempt("1.2.3.4") for _ in range(5)]

    veredictos = asyncio.run(_run())
    assert veredictos == [True, True, True, False, False]
    # La ventana caduca (no es un baneo permanente).
    assert fake.expires  # se puso TTL al primer intento


def test_ws_rate_limit_no_tira_llamadas_si_redis_cae(monkeypatch):
    """El límite es defensa AÑADIDA; la autenticación real es is_call_authentic.
    Si Redis no está, no se rechazan llamadas legítimas."""
    from app.providers.voice import retell as retell_mod

    def _broken():
        raise RuntimeError("Redis caído")

    monkeypatch.setattr(retell_mod, "get_redis", _broken)
    assert asyncio.run(retell_mod.register_ws_attempt("1.2.3.4")) is True


def test_comentario_de_RETELL_WS_VALIDATE_ya_no_invita_a_apagarlo():
    """B5: el comentario decía "ponlo en false, fail-open ya cubre los fallos
    transitorios". El código es FAIL-CLOSED desde hace tiempo, así que ese
    consejo dejaba el WebSocket abierto a internet."""
    from pathlib import Path

    cfg = Path(__file__).resolve().parents[1] / "app" / "core" / "config.py"
    texto = cfg.read_text(encoding="utf-8")
    bloque = texto.split("RETELL_WS_VALIDATE")[0][-2500:]
    assert "NO LO APAGUES" in bloque
    assert "FAIL-CLOSED" in bloque
    # El consejo viejo solo puede aparecer para desmentirlo.
    if "fail-open ya cubre" in bloque:
        assert "YA NO ES CIERTO" in bloque
    # Advierte de LO QUE SE ESTÁ APAGANDO, no solo de que no se apague.
    assert "agendar citas reales" in bloque

    env = Path(__file__).resolve().parents[2] / ".env.desarrollo.example"
    env_txt = env.read_text(encoding="utf-8")
    assert "Ponlo en false solo si diera problemas" not in env_txt
    assert "NO LO APAGUES" in env_txt


def test_csp_declara_media_src():
    """B4: `default-src 'self'` sin `media-src` bloquea la grabación de Retell
    (dominio externo) en el <audio> de la página Llamadas."""
    from pathlib import Path

    conf = Path(__file__).resolve().parents[2] / "frontend" / "nginx.conf"
    # OJO: la cabecera del fichero explica en un COMENTARIO cómo estrechar la
    # política por despliegue, y ese ejemplo también contiene la cadena
    # "Content-Security-Policy". Si no se descartan las líneas comentadas, el
    # test acaba mirando el ejemplo (que solo declara connect-src) en vez de la
    # política real y falla sin motivo. Se ignoran las líneas comentadas.
    activas = [
        ln for ln in conf.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#")
    ]
    # La política real vive en el `map $host $csp_policy`; los `add_header` solo
    # la referencian por variable. Se juntan todas las líneas activas que
    # declaran directivas CSP y se comprueba sobre el conjunto.
    csp = [ln for ln in activas if "Content-Security-Policy" in ln or "default-src" in ln]
    assert csp, "no se encontró la cabecera CSP"
    politica = "\n".join(csp)
    assert "media-src" in politica
    assert "media-src 'self' data: blob: https:" in politica


# ---------------------------------------------------------------------------
# Frenos del panel (con DB)
# ---------------------------------------------------------------------------


async def _cleanup(contact_ids: list, phones: list[str] | None = None):
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.contact import Contact

    async with db_session() as db:
        for cid in contact_ids:
            c = (await db.execute(select(Contact).where(Contact.id == cid))).scalar_one_or_none()
            if c:
                await db.delete(c)  # cascade borra conversaciones y mensajes
        for phone in phones or []:
            c = (
                await db.execute(select(Contact).where(Contact.telefono == phone))
            ).scalar_one_or_none()
            if c:
                await db.delete(c)
        await db.commit()


def _patch_common(monkeypatch, *, agent=None, paused=False, training=False, flagged=False):
    """Deja el canal en un estado concreto y devuelve la lista de llamadas al
    agente (para comprobar que NO se ejecuta cuando está frenado)."""
    from app.api import voice
    from app.services.moderation import ModerationResult

    _silence_side_effects(monkeypatch)
    llamadas: list[str] = []

    async def _fake_runtime(_canal):
        return agent if agent is not None else _runtime()

    async def _fake_run_agent(**kwargs):
        llamadas.append(kwargs.get("user_message", ""))
        return "respuesta del agente"

    async def _fake_paused(_canal):
        return paused

    async def _fake_training(_canal):
        return training

    async def _fake_whitelist(_conv_id):
        return False

    async def _fake_moderate(_text):
        return ModerationResult(flagged=flagged, categories=["odio"] if flagged else [])

    monkeypatch.setattr(voice, "get_runtime_for_channel", _fake_runtime)
    monkeypatch.setattr(voice, "run_agent", _fake_run_agent)
    monkeypatch.setattr(voice, "is_channel_paused", _fake_paused)
    monkeypatch.setattr(voice, "is_channel_training", _fake_training)
    monkeypatch.setattr(voice, "is_in_demo_whitelist", _fake_whitelist)
    monkeypatch.setattr(voice, "moderate", _fake_moderate)
    return llamadas


def _turn(call_id: str, text: str = "Hola, quiero una cita", **kw):
    from app.api.voice import _run_retell_turn

    transcript = [{"role": "user", "content": text}]
    return asyncio.run(
        _run_retell_turn(call_id, text, transcript, upto=0, **kw)
    )


@pytestmark_db
def test_pausa_global_apaga_la_voz(monkeypatch):
    """B1 — EL FALLO GORDO. `budget.py` aplica el tope mensual con
    `set_agent_paused(True)`, que es pausa GLOBAL. Como la voz no la miraba, al
    superarse el presupuesto se apagaba todo MENOS el canal más caro."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation, ConversationCanal

    llamadas = _patch_common(monkeypatch, paused=True)
    call_id = f"call_{uuid.uuid4().hex[:10]}"
    result = _turn(call_id)

    assert llamadas == [], "el agente se ha ejecutado con el canal pausado"
    assert result.end_call is True, "una llamada que no se puede atender debe colgarse"
    assert "no puedo atenderte" in result.text.lower()

    async def _check_and_clean():
        async with db_session() as db:
            conv = (
                await db.execute(
                    select(Conversation).where(
                        Conversation.session_id == call_id,
                        Conversation.canal == ConversationCanal.retell_voice,
                    )
                )
            ).scalar_one()
            # La transcripción SÍ se guarda aunque el bot no conteste.
            return conv.contact_id

    contact_id = asyncio.run(_check_and_clean())
    asyncio.run(_cleanup([contact_id]))


@pytestmark_db
def test_operador_toma_la_llamada_y_el_bot_calla(monkeypatch):
    """B2 — si un operador toma la conversación desde la bandeja mientras la
    llamada sigue viva, el bot seguía hablando por teléfono."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation, ConversationCanal, ConversationStatus

    llamadas = _patch_common(monkeypatch)
    call_id = f"call_{uuid.uuid4().hex[:10]}"

    first = _turn(call_id, "Hola")
    assert llamadas == ["Hola"]
    assert first.text == "respuesta del agente"

    async def _tomar():
        async with db_session() as db:
            conv = (
                await db.execute(
                    select(Conversation).where(
                        Conversation.session_id == call_id,
                        Conversation.canal == ConversationCanal.retell_voice,
                    )
                )
            ).scalar_one()
            conv.status = ConversationStatus.humano
            await db.commit()
            return conv.contact_id

    contact_id = asyncio.run(_tomar())

    second = _turn(call_id, "¿Sigues ahí?")
    assert llamadas == ["Hola"], "el bot ha seguido hablando con la llamada tomada"
    assert second.end_call is True

    asyncio.run(_cleanup([contact_id]))


@pytestmark_db
def test_cuarentena_frena_la_voz(monkeypatch):
    """B2 — `conv.quarantined_at` no se miraba en voz."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation, ConversationCanal

    llamadas = _patch_common(monkeypatch)
    call_id = f"call_{uuid.uuid4().hex[:10]}"
    _turn(call_id, "Hola")
    assert len(llamadas) == 1

    async def _cuarentena():
        from datetime import datetime, timezone

        async with db_session() as db:
            conv = (
                await db.execute(
                    select(Conversation).where(Conversation.session_id == call_id)
                )
            ).scalar_one()
            conv.quarantined_at = datetime.now(timezone.utc)
            await db.commit()
            return conv.contact_id

    contact_id = asyncio.run(_cuarentena())
    r = _turn(call_id, "otra cosa")
    assert len(llamadas) == 1, "el agente corrió con la conversación en cuarentena"
    assert r.end_call is True
    asyncio.run(_cleanup([contact_id]))


@pytestmark_db
def test_moderacion_deriva_y_calla(monkeypatch):
    """B2 — `moderate(...)` no se llamaba en voz."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation, ConversationStatus

    llamadas = _patch_common(monkeypatch, flagged=True)
    call_id = f"call_{uuid.uuid4().hex[:10]}"
    r = _turn(call_id, "contenido marcado")

    assert llamadas == [], "el agente corrió con el turno bloqueado por moderación"
    assert r.end_call is True

    async def _leer():
        async with db_session() as db:
            conv = (
                await db.execute(
                    select(Conversation).where(Conversation.session_id == call_id)
                )
            ).scalar_one()
            return conv.status, conv.contact_id

    status, contact_id = asyncio.run(_leer())
    assert status == ConversationStatus.humano
    asyncio.run(_cleanup([contact_id]))


@pytestmark_db
def test_sin_agente_configurado(monkeypatch):
    """I7 — se le decía a quien llamaba "ahora no puedo atenderte" y no quedaba
    ningún rastro en "Logs en vivo"."""
    from app.api import voice

    _patch_common(monkeypatch)
    logs: list[dict] = []

    async def _capture(**kwargs):
        logs.append(kwargs)

    async def _no_agent(_canal):
        return None

    monkeypatch.setattr(voice, "get_runtime_for_channel", _no_agent)
    monkeypatch.setattr(voice, "push_runtime_log", _capture)

    call_id = f"call_{uuid.uuid4().hex[:10]}"
    r = _turn(call_id, "Hola")
    assert r.end_call is True
    assert any(entry.get("event") == "voice.no_agent" for entry in logs)

    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation

    async def _cid():
        async with db_session() as db:
            conv = (
                await db.execute(select(Conversation).where(Conversation.session_id == call_id))
            ).scalar_one()
            return conv.contact_id

    asyncio.run(_cleanup([asyncio.run(_cid())]))


@pytestmark_db
def test_entrenamiento_no_habla_pero_deja_sugerencia(monkeypatch):
    """El tercer botón de la tarjeta del canal tampoco hacía nada."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.conversation import Conversation
    from app.models.message import Message, MessageRole

    llamadas = _patch_common(monkeypatch, training=True)
    call_id = f"call_{uuid.uuid4().hex[:10]}"
    r = _turn(call_id, "Hola")

    assert llamadas == ["Hola"], "en Entrenamiento el agente SÍ debe pensar la respuesta"
    assert r.text != "respuesta del agente", "en Entrenamiento no se le dice al que llama"

    async def _leer():
        async with db_session() as db:
            conv = (
                await db.execute(select(Conversation).where(Conversation.session_id == call_id))
            ).scalar_one()
            msgs = (
                await db.execute(
                    select(Message).where(
                        Message.conversation_id == conv.id,
                        Message.rol == MessageRole.assistant,
                    )
                )
            ).scalars().all()
            return conv.contact_id, [m.extra for m in msgs]

    contact_id, extras = asyncio.run(_leer())
    assert any(e.get("is_draft") and not e.get("draft_sent") for e in extras)
    asyncio.run(_cleanup([contact_id]))


@pytestmark_db
def test_llamadas_del_mismo_numero_van_a_la_misma_ficha(monkeypatch):
    """I3 — el mismo cliente llamando tres veces creaba tres fichas con un
    "teléfono" tipo `voice:call_3a9f`."""
    from sqlalchemy import func, select

    from app.db.session import db_session
    from app.models.contact import Contact
    from app.models.conversation import Conversation

    _patch_common(monkeypatch)
    phone = f"+34600{uuid.uuid4().int % 1000000:06d}"
    call_a = f"call_{uuid.uuid4().hex[:10]}"
    call_b = f"call_{uuid.uuid4().hex[:10]}"

    _turn(call_a, "Primera llamada", from_number=phone)
    _turn(call_b, "Segunda llamada", from_number=phone)

    async def _leer():
        async with db_session() as db:
            contactos = (
                await db.execute(select(Contact).where(Contact.telefono == phone))
            ).scalars().all()
            convs = (
                await db.execute(
                    select(func.count())
                    .select_from(Conversation)
                    .where(Conversation.session_id.in_([call_a, call_b]))
                )
            ).scalar_one()
            return contactos, int(convs)

    contactos, n_convs = asyncio.run(_leer())
    assert len(contactos) == 1, "cada llamada creó una ficha nueva"
    # Una conversación POR LLAMADA (no se mezclan dos llamadas en un hilo).
    assert n_convs == 2
    asyncio.run(_cleanup([contactos[0].id]))


@pytestmark_db
def test_no_se_cuela_en_la_conversacion_de_otro_canal(monkeypatch):
    """B2 — la conversación se buscaba SIN filtrar canal ni llamada: con el
    teléfono real, la llamada se habría metido dentro del hilo de WhatsApp del
    mismo cliente."""
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal, ConversationStatus

    _patch_common(monkeypatch)
    phone = f"+34600{uuid.uuid4().int % 1000000:06d}"

    async def _prep():
        async with db_session() as db:
            c = Contact(telefono=phone, origen=ContactOrigen.manual, in_crm=False)
            db.add(c)
            await db.flush()
            wa = Conversation(
                contact_id=c.id,
                canal=ConversationCanal.whatsapp,
                session_id=phone,
                status=ConversationStatus.bot,
            )
            db.add(wa)
            await db.commit()
            return c.id, wa.id

    contact_id, wa_id = asyncio.run(_prep())
    call_id = f"call_{uuid.uuid4().hex[:10]}"
    _turn(call_id, "Hola", from_number=phone)

    async def _leer():
        async with db_session() as db:
            conv = (
                await db.execute(select(Conversation).where(Conversation.session_id == call_id))
            ).scalar_one()
            return conv.id, conv.canal

    conv_id, canal = asyncio.run(_leer())
    assert conv_id != wa_id, "la llamada se metió en el hilo de WhatsApp"
    assert canal == ConversationCanal.retell_voice
    asyncio.run(_cleanup([contact_id]))


@pytestmark_db
def test_al_colgar_la_conversacion_se_archiva(monkeypatch):
    """B3 — el archivado vivía SOLO en el webhook. Sin esa URL puesta en Retell
    (que hasta ahora ni se enseñaba), cada llamada dejaba una conversación viva
    para siempre en "Activas"."""
    from sqlalchemy import select

    from app.api.voice import _close_call_conversation
    from app.db.session import db_session
    from app.models.conversation import Conversation

    _patch_common(monkeypatch)
    call_id = f"call_{uuid.uuid4().hex[:10]}"
    _turn(call_id, "Hola")
    asyncio.run(_close_call_conversation(call_id))

    async def _leer():
        async with db_session() as db:
            conv = (
                await db.execute(select(Conversation).where(Conversation.session_id == call_id))
            ).scalar_one()
            return conv.archived_at, conv.ended_at, conv.contact_id

    archived_at, ended_at, contact_id = asyncio.run(_leer())
    assert archived_at is not None
    assert ended_at is not None
    asyncio.run(_cleanup([contact_id]))


@pytestmark_db
def test_relink_contacto_por_telefono_del_webhook(monkeypatch):
    """I3 — el webhook de fin también trae `from_number`: si el WebSocket no vio
    el `call_details`, se reengancha la llamada al contacto de verdad."""
    from sqlalchemy import select

    from app.api.voice import _relink_contact_by_phone
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation

    _patch_common(monkeypatch)
    phone = f"+34600{uuid.uuid4().int % 1000000:06d}"
    call_id = f"call_{uuid.uuid4().hex[:10]}"

    async def _prep():
        # Contacto real preexistente (p. ej. de WhatsApp).
        async with db_session() as db:
            real = Contact(telefono=phone, origen=ContactOrigen.manual, in_crm=True)
            db.add(real)
            await db.commit()
            return real.id

    real_id = asyncio.run(_prep())
    # Llamada SIN identificación en el WS → ficha sintética.
    _turn(call_id, "Hola")

    async def _relink():
        async with db_session() as db:
            conv = (
                await db.execute(select(Conversation).where(Conversation.session_id == call_id))
            ).scalar_one()
            sintetico_id = conv.contact_id
            await _relink_contact_by_phone(db, conv, phone)
            await db.commit()
            return sintetico_id

    sintetico_id = asyncio.run(_relink())

    async def _leer():
        async with db_session() as db:
            conv = (
                await db.execute(select(Conversation).where(Conversation.session_id == call_id))
            ).scalar_one()
            sintetico = (
                await db.execute(select(Contact).where(Contact.id == sintetico_id))
            ).scalar_one_or_none()
            return conv.contact_id, sintetico

    nuevo_contact_id, sintetico = asyncio.run(_leer())
    assert nuevo_contact_id == real_id
    assert sintetico is None, "la ficha sintética huérfana debería borrarse"
    asyncio.run(_cleanup([real_id]))


# ---------------------------------------------------------------------------
# WebSocket: pings, silencios y turnos que no bloquean (I1 / I9)
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    """WebSocket de mentira: se le da un guion de mensajes y recoge los envíos."""

    def __init__(self, guion: list[dict]) -> None:
        self._guion = list(guion)
        self.sent: list[dict] = []
        self.accepted = False
        self.closed_code: int | None = None
        self.client = type("C", (), {"host": "1.2.3.4"})()
        self.headers: dict = {}

    async def accept(self):
        self.accepted = True

    async def close(self, code: int = 1000):
        self.closed_code = code

    async def send_json(self, payload):
        self.sent.append(payload)

    async def receive_json(self):
        from fastapi import WebSocketDisconnect

        if not self._guion:
            raise WebSocketDisconnect(1000)
        item = self._guion.pop(0)
        # Deja correr al event loop para que las tareas de turno avancen.
        await asyncio.sleep(0)
        return item


def _ws_env(monkeypatch, turnos: list[str]):
    """Deja el WebSocket listo para correr sin BD, sin Retell y sin Redis."""
    from app.api import voice

    _silence_side_effects(monkeypatch)

    async def _authentic(_call_id):
        return True

    async def _attempt(_ip):
        return True

    async def _close_conv(_call_id):
        return None

    async def _fake_turn(call_id, text, transcript, **kw):
        turnos.append(text)
        return voice.TurnResult(f"eco: {text}")

    monkeypatch.setattr(voice, "is_call_authentic", _authentic)
    monkeypatch.setattr(voice, "register_ws_attempt", _attempt)
    monkeypatch.setattr(voice, "_close_call_conversation", _close_conv)
    monkeypatch.setattr(voice, "_run_retell_turn", _fake_turn)


def test_ws_silencio_no_repite_ni_paga_el_turno(monkeypatch):
    """I1 — `reminder_required` iba por el mismo camino que `response_required`,
    así que se re-contestaba el ÚLTIMO turno del usuario (que ya se había
    contestado): quien llamaba oía la respuesta anterior palabra por palabra, la
    transcripción salía duplicada y se pagaba un turno entero por cada silencio."""
    from app.api import voice

    turnos: list[str] = []
    _ws_env(monkeypatch, turnos)

    transcript = [{"role": "user", "content": "quiero cita"}]
    ws = _FakeWebSocket(
        [
            {"interaction_type": "response_required", "response_id": 1, "transcript": transcript},
            {"interaction_type": "reminder_required", "response_id": 2, "transcript": transcript},
            {"interaction_type": "reminder_required", "response_id": 3, "transcript": transcript},
        ]
    )
    asyncio.run(voice.retell_llm_ws(ws, "call_x"))

    assert turnos == ["quiero cita"], f"un silencio volvió a ejecutar el agente: {turnos}"
    respuestas = [m for m in ws.sent if m.get("response_type") == "response"]
    # saludo + 1 turno + 2 recordatorios
    assert len(respuestas) == 4
    assert respuestas[2]["content"] == voice.VOICE_TEXTS["reminder_message"]
    assert respuestas[2]["content"] != respuestas[1]["content"]


def test_ws_contesta_los_pings_sin_esperar_al_modelo(monkeypatch):
    """I9 — el bucle era estrictamente secuencial: mientras se esperaba al
    modelo no se leía nada del socket, así que el pong de Retell salía tarde y
    la llamada moría con "conexión perdida"."""
    from app.api import voice

    _silence_side_effects(monkeypatch)
    empezado = asyncio.Event()
    soltar = asyncio.Event()

    async def _authentic(_c):
        return True

    async def _attempt(_ip):
        return True

    async def _close_conv(_c):
        return None

    async def _slow_turn(call_id, text, transcript, **kw):
        empezado.set()
        await soltar.wait()  # el modelo "tarda"
        return voice.TurnResult("por fin")

    monkeypatch.setattr(voice, "is_call_authentic", _authentic)
    monkeypatch.setattr(voice, "register_ws_attempt", _attempt)
    monkeypatch.setattr(voice, "_close_call_conversation", _close_conv)
    monkeypatch.setattr(voice, "_run_retell_turn", _slow_turn)

    class _WS(_FakeWebSocket):
        async def receive_json(self):
            from fastapi import WebSocketDisconnect

            if not self._guion:
                soltar.set()
                raise WebSocketDisconnect(1000)
            item = self._guion.pop(0)
            if item.get("interaction_type") == "ping_pong":
                # El ping llega MIENTRAS el turno anterior sigue en el modelo.
                await empezado.wait()
            await asyncio.sleep(0)
            return item

    ws = _WS(
        [
            {
                "interaction_type": "response_required",
                "response_id": 1,
                "transcript": [{"role": "user", "content": "hola"}],
            },
            {"interaction_type": "ping_pong", "timestamp": 12345},
        ]
    )
    asyncio.run(asyncio.wait_for(voice.retell_llm_ws(ws, "call_y"), timeout=5))

    pongs = [m for m in ws.sent if m.get("response_type") == "ping_pong"]
    assert pongs, "el pong no salió mientras el modelo pensaba: Retell colgaría"
    assert pongs[0]["timestamp"] == 12345


def test_ws_guarda_el_telefono_de_call_details(monkeypatch):
    """I3 — a Retell se le pedía `call_details` expresamente y el handler tiraba
    ese mensaje a la basura."""
    from app.api import voice

    _silence_side_effects(monkeypatch)
    vistos: list = []

    async def _authentic(_c):
        return True

    async def _attempt(_ip):
        return True

    async def _close_conv(_c):
        return None

    async def _turn(call_id, text, transcript, **kw):
        vistos.append(kw.get("from_number"))
        return voice.TurnResult("ok")

    monkeypatch.setattr(voice, "is_call_authentic", _authentic)
    monkeypatch.setattr(voice, "register_ws_attempt", _attempt)
    monkeypatch.setattr(voice, "_close_call_conversation", _close_conv)
    monkeypatch.setattr(voice, "_run_retell_turn", _turn)

    ws = _FakeWebSocket(
        [
            {"interaction_type": "call_details", "call": {"from_number": "+34600111222"}},
            {
                "interaction_type": "response_required",
                "response_id": 1,
                "transcript": [{"role": "user", "content": "hola"}],
            },
        ]
    )
    asyncio.run(voice.retell_llm_ws(ws, "call_z"))
    assert vistos == ["+34600111222"]


def test_ws_rechaza_si_no_valida(monkeypatch):
    """La validación sigue siendo la puerta: si no valida, ni se acepta."""
    from app.api import voice

    _silence_side_effects(monkeypatch)

    async def _no(_c):
        return False

    async def _attempt(_ip):
        return True

    monkeypatch.setattr(voice, "is_call_authentic", _no)
    monkeypatch.setattr(voice, "register_ws_attempt", _attempt)

    ws = _FakeWebSocket([])
    asyncio.run(voice.retell_llm_ws(ws, "call_falso"))
    assert ws.accepted is False
    assert ws.closed_code == 1008


def test_ws_rechaza_por_tope_de_intentos(monkeypatch):
    """B5 — y sin gastar una petición a la API de Retell."""
    from app.api import voice

    _silence_side_effects(monkeypatch)
    validaciones: list[str] = []

    async def _authentic(call_id):
        validaciones.append(call_id)
        return True

    async def _attempt(_ip):
        return False

    monkeypatch.setattr(voice, "is_call_authentic", _authentic)
    monkeypatch.setattr(voice, "register_ws_attempt", _attempt)

    ws = _FakeWebSocket([])
    asyncio.run(voice.retell_llm_ws(ws, "call_abuso"))
    assert ws.accepted is False
    assert ws.closed_code == 1008
    assert validaciones == [], "se consultó a Retell pese a superar el tope"


def test_ws_tope_de_llamadas_simultaneas(monkeypatch):
    """I9 — sin tope, un pico agota el event loop y el pool de BD."""
    from app.api import voice
    from app.core.config import settings

    _silence_side_effects(monkeypatch)

    async def _authentic(_c):
        return True

    async def _attempt(_ip):
        return True

    monkeypatch.setattr(voice, "is_call_authentic", _authentic)
    monkeypatch.setattr(voice, "register_ws_attempt", _attempt)
    monkeypatch.setattr(settings, "RETELL_MAX_CONCURRENT_CALLS", 0)

    ws = _FakeWebSocket([])
    asyncio.run(voice.retell_llm_ws(ws, "call_lleno"))
    assert ws.accepted is False
    assert ws.closed_code == 1013
