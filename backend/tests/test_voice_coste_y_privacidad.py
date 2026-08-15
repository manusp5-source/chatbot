"""Voz: los minutos cuestan dinero, la voz del cliente es PII y el borrado borra.

Los tres agujeros que quedaban abiertos en el canal Retell:

  I10 — Los MINUTOS no se contabilizaban en ninguna parte. El tope mensual de
        gasto medía tokens del modelo, así que del canal MÁS CARO solo vigilaba
        la mitad barata, y el cliente no tenía pantalla donde ver lo que le
        cuesta la voz. Ahora el coste se guarda por llamada (prefiriendo el
        importe REAL que factura Retell sobre la estimación por tarifa) y suma
        al tope, global y por agente.

  I11 — La transcripción de la llamada se guardaba EN CLARO en
        `messages.contenido`, mientras `audio_transcript` —justo al lado— iba
        cifrado con un comentario diciendo que es el dato de máxima
        sensibilidad. Es decir: una nota de voz de WhatsApp de cinco segundos
        iba cifrada y una llamada de teléfono entera no. La voz del cliente
        pasa al campo cifrado. Y con ella la URL de la grabación, que mientras
        `opt_in_signed_url` no esté activo es pública y sin caducidad.

  I12 — El derecho de supresión no tocaba el audio de las llamadas: seguía en
        Retell para siempre. Faltaba el borrado por API.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

# ---------------------------------------------------------------------------
# DB-gate (mismo patrón que test_voice_gating.py)
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


async def _borra_contacto(contact_id) -> None:
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.contact import Contact

    async with db_session() as db:
        c = (
            await db.execute(select(Contact).where(Contact.id == contact_id))
        ).scalar_one_or_none()
        if c:
            await db.delete(c)  # cascade: conversaciones y mensajes
            await db.commit()


# ===========================================================================
# I10 — Cómo se modela el coste de los minutos
# ===========================================================================


def test_el_coste_real_de_retell_gana_a_la_estimacion(monkeypatch):
    """Retell manda lo que factura de verdad; nuestra tarifa es solo el plan B.

    `combined_cost` viene en CENTAVOS
    (https://docs.retellai.com/api-references/get-call), así que hay que
    dividir entre 100 — devolverlo tal cual multiplicaría el gasto por cien.
    """
    from app.api import voice
    from app.core.config import settings

    monkeypatch.setattr(settings, "RETELL_PRICE_PER_MINUTE_USD", 0.30, raising=False)
    # 123 centavos = 1.23 $. La estimación (2 min × 0.30) daría 0.60: distinta
    # a propósito, para que no se pueda pasar por buena la que no es.
    call = {"call_cost": {"combined_cost": 123}, "duration_ms": 120_000}

    coste, origen = voice._telephony_cost(call, 120)
    assert coste == 1.23, "el coste de Retell viene en centavos, no en dólares"
    assert origen == "retell"


def test_sin_coste_de_retell_se_estima_con_la_tarifa(monkeypatch):
    """Webhook sin desglose de coste: se estima con la tarifa por minuto."""
    from app.api import voice
    from app.core.config import settings

    monkeypatch.setattr(settings, "RETELL_PRICE_PER_MINUTE_USD", 0.30, raising=False)

    coste, origen = voice._telephony_cost({}, 120)  # 2 minutos
    assert coste == 0.60
    assert origen == "estimado"


def test_sin_coste_ni_tarifa_no_se_inventa_un_cero(monkeypatch):
    """Un cero inventado es peor que un hueco: le diría al tope de gasto que la
    telefonía sale gratis. Sin dato, no hay dato — y el log lo avisa."""
    from app.api import voice
    from app.core.config import settings

    monkeypatch.setattr(settings, "RETELL_PRICE_PER_MINUTE_USD", 0.0, raising=False)

    assert voice._telephony_cost({}, 120) == (None, None)


def test_un_cero_facturado_por_retell_si_es_un_dato(monkeypatch):
    """0 centavos de Retell es un coste real (llamada de 2s, plan con minutos
    incluidos), no un "no lo sé". Se guarda como tal."""
    from app.api import voice
    from app.core.config import settings

    monkeypatch.setattr(settings, "RETELL_PRICE_PER_MINUTE_USD", 0.30, raising=False)

    coste, origen = voice._telephony_cost({"call_cost": {"combined_cost": 0}}, 2)
    assert coste == 0.0 and origen == "retell"


def test_la_estimacion_no_pisa_lo_que_retell_ya_facturo():
    """`call_analyzed` llega DESPUÉS de `call_ended`. Si el primero trajo el
    importe real y el segundo no, la estimación no puede borrarlo."""
    import inspect

    from app.api import voice

    src = inspect.getsource(voice.retell_call_webhook)
    assert 'cost_source == "estimado"' in src and 'conv.call_cost_source == "retell"' in src, (
        "el webhook debe proteger el coste real de Retell frente a una estimación posterior"
    )


@pytestmark_db
def test_el_webhook_guarda_el_coste_y_no_solo_lo_loguea():
    """I10 — el coste se calculaba y se tiraba a "Logs en vivo". Ahora se
    PERSISTE: sin fila en BD el tope mensual no puede sumarlo."""
    from sqlalchemy import select

    from app.api.voice import _telephony_cost
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal

    call_id = f"call_{uuid.uuid4().hex[:10]}"

    async def _flujo():
        async with db_session() as db:
            contact = Contact(
                telefono=f"voice:{call_id}", origen=ContactOrigen.manual, in_crm=False
            )
            db.add(contact)
            await db.flush()
            conv = Conversation(
                contact_id=contact.id,
                canal=ConversationCanal.retell_voice,
                session_id=call_id,
            )
            db.add(conv)
            await db.commit()
            cid = contact.id

        # Lo que hace el webhook con el payload de Retell.
        call = {"call_cost": {"combined_cost": 250}, "duration_ms": 90_000}
        coste, origen = _telephony_cost(call, 90)
        async with db_session() as db:
            conv = (
                await db.execute(
                    select(Conversation).where(Conversation.session_id == call_id)
                )
            ).scalar_one()
            conv.call_cost_usd = coste
            conv.call_cost_source = origen
            conv.call_duration_seconds = 90
            await db.commit()

        async with db_session() as db:
            conv = (
                await db.execute(
                    select(Conversation).where(Conversation.session_id == call_id)
                )
            ).scalar_one()
            return cid, float(conv.call_cost_usd), conv.call_cost_source

    cid, coste, origen = asyncio.run(_flujo())
    assert coste == 2.50
    assert origen == "retell"
    asyncio.run(_borra_contacto(cid))


# ===========================================================================
# I10 — El tope mensual de gasto ve los minutos
# ===========================================================================


@pytestmark_db
def test_los_minutos_de_voz_suman_al_tope_mensual():
    """El agujero de fondo: el tope medía tokens y la voz se le colaba entera.

    Se comprueba que el coste del mes SUBE exactamente lo que cuesta la llamada
    que acabamos de meter.
    """
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal
    from app.services.budget import current_month_cost_usd, voice_month_telephony_usage

    call_id = f"call_{uuid.uuid4().hex[:10]}"

    async def _flujo():
        antes = await current_month_cost_usd()
        async with db_session() as db:
            contact = Contact(
                telefono=f"voice:{call_id}", origen=ContactOrigen.manual, in_crm=False
            )
            db.add(contact)
            await db.flush()
            db.add(
                Conversation(
                    contact_id=contact.id,
                    canal=ConversationCanal.retell_voice,
                    session_id=call_id,
                    ended_at=datetime.now(UTC),
                    call_duration_seconds=180,
                    call_cost_usd=4.25,
                    call_cost_source="retell",
                )
            )
            await db.commit()
            cid = contact.id
        despues = await current_month_cost_usd()
        uso = await voice_month_telephony_usage()
        return cid, antes, despues, uso

    cid, antes, despues, uso = asyncio.run(_flujo())
    assert round(despues - antes, 4) == 4.25, "los minutos de la llamada no suman al tope"
    assert uso["cost_usd"] >= 4.25
    assert uso["minutes"] >= 3.0
    asyncio.run(_borra_contacto(cid))


@pytestmark_db
def test_una_llamada_del_mes_pasado_no_cuenta_en_este():
    """El tope es MENSUAL. Una llamada vieja no puede pausar el bot hoy."""
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal
    from app.services.budget import voice_month_telephony_usage

    call_id = f"call_{uuid.uuid4().hex[:10]}"
    now = datetime.now(UTC)
    mes_pasado = datetime(now.year, now.month, 1, tzinfo=UTC) - timedelta(days=5)

    async def _flujo():
        antes = (await voice_month_telephony_usage())["cost_usd"]
        async with db_session() as db:
            contact = Contact(
                telefono=f"voice:{call_id}", origen=ContactOrigen.manual, in_crm=False
            )
            db.add(contact)
            await db.flush()
            db.add(
                Conversation(
                    contact_id=contact.id,
                    canal=ConversationCanal.retell_voice,
                    session_id=call_id,
                    started_at=mes_pasado,
                    ended_at=mes_pasado,
                    call_duration_seconds=600,
                    call_cost_usd=99.0,
                    call_cost_source="retell",
                )
            )
            await db.commit()
            cid = contact.id
        return cid, antes, (await voice_month_telephony_usage())["cost_usd"]

    cid, antes, despues = asyncio.run(_flujo())
    assert despues == antes, "una llamada del mes pasado ha entrado en el gasto de este mes"
    asyncio.run(_borra_contacto(cid))


@pytestmark_db
def test_las_llamadas_sin_valorar_se_cuentan_aparte():
    """Una llamada con duración pero sin importe (ni Retell ni tarifa) deja el
    total INCOMPLETO. Se dice cuántas son en vez de dar el total por bueno."""
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal
    from app.services.budget import voice_month_telephony_usage

    call_id = f"call_{uuid.uuid4().hex[:10]}"

    async def _flujo():
        antes = (await voice_month_telephony_usage())["calls_without_cost"]
        async with db_session() as db:
            contact = Contact(
                telefono=f"voice:{call_id}", origen=ContactOrigen.manual, in_crm=False
            )
            db.add(contact)
            await db.flush()
            db.add(
                Conversation(
                    contact_id=contact.id,
                    canal=ConversationCanal.retell_voice,
                    session_id=call_id,
                    ended_at=datetime.now(UTC),
                    call_duration_seconds=240,
                    call_cost_usd=None,
                )
            )
            await db.commit()
            cid = contact.id
        return cid, antes, (await voice_month_telephony_usage())["calls_without_cost"]

    cid, antes, despues = asyncio.run(_flujo())
    assert despues == antes + 1
    asyncio.run(_borra_contacto(cid))


@pytestmark_db
def test_el_tope_por_agente_tambien_ve_los_minutos():
    """El tope propio del agente desactiva SOLO a ese agente. Si no contase los
    minutos, un agente de voz podría pasarse de su presupuesto sin enterarse:
    el mismo agujero del tope global, a menor escala.

    La llamada se le imputa al agente que la contestó, que es el que dejó su
    `agent_id` en `llm_usage_log` junto al `conversation_id` de la llamada.
    """
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal
    from app.models.llm_usage import LLMUsage
    from app.services.budget import agent_month_cost_usd

    call_id = f"call_{uuid.uuid4().hex[:10]}"
    agent_id = uuid.uuid4()
    otro_agente = uuid.uuid4()

    async def _flujo():
        async with db_session() as db:
            contact = Contact(
                telefono=f"voice:{call_id}", origen=ContactOrigen.manual, in_crm=False
            )
            db.add(contact)
            await db.flush()
            conv = Conversation(
                contact_id=contact.id,
                canal=ConversationCanal.retell_voice,
                session_id=call_id,
                ended_at=datetime.now(UTC),
                call_duration_seconds=120,
                call_cost_usd=3.0,
                call_cost_source="retell",
            )
            db.add(conv)
            await db.flush()
            db.add(
                LLMUsage(
                    source="agent",
                    model="gpt-test",
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=0,
                    conversation_id=conv.id,
                    agent_id=agent_id,
                )
            )
            await db.commit()
            cid = contact.id
        return cid, await agent_month_cost_usd(agent_id), await agent_month_cost_usd(otro_agente)

    cid, mio, ajeno = asyncio.run(_flujo())
    assert mio >= 3.0, "los minutos de la llamada no se imputan al agente que la atendió"
    assert ajeno == 0.0, "los minutos se le han imputado a un agente que no atendió la llamada"
    asyncio.run(_borra_contacto(cid))


def test_los_minutos_no_se_cuelan_en_la_tabla_de_precios_por_token():
    """Decisión de modelado: el minuto NO es un token.

    `llm_model_price` son tarifas por millón de tokens y `llm_usage_log` cuenta
    tokens. Meter ahí unos "tokens" inventados para que cuadrase la aritmética
    habría falseado el desglose de consumo por modelo del panel. El minuto va
    como coste plano en la conversación.
    """
    import inspect

    from app.services import budget

    src = inspect.getsource(budget)
    assert "conversations" in src and "call_cost_usd" in src
    # El sumando de telefonía no pasa por la función de precios de tokens.
    assert "estimate_cost_usd(r.model" in src
    assert "estimate_cost_usd" not in src.split("_VOICE_MONTH_SQL")[1].split("async def current_month_cost_usd")[0]


# ===========================================================================
# I11 — La voz del cliente se guarda cifrada
# ===========================================================================


@pytestmark_db
def test_la_transcripcion_de_la_llamada_se_guarda_cifrada(monkeypatch):
    """El turno del cliente NO puede quedar en `contenido` (texto plano).

    Se mira la BD EN CRUDO: no basta con que el ORM devuelva el texto (lo
    devuelve igual, descifrándolo), hay que comprobar que lo que hay escrito en
    disco no es la frase del cliente.
    """
    from sqlalchemy import text as sql_text

    import tests.test_voice_gating as vg
    from app.db.session import db_session
    from app.models.conversation import ConversationCanal

    vg._patch_common(monkeypatch)
    call_id = f"call_{uuid.uuid4().hex[:10]}"
    secreto = f"Me llamo Ana y mi DNI es {uuid.uuid4().hex[:8].upper()}"
    vg._turn(call_id, secreto)

    async def _leer():
        async with db_session() as db:
            row = (
                await db.execute(
                    sql_text(
                        """
                        SELECT m.contenido AS claro,
                               m.audio_transcript AS cifrado,
                               c.contact_id AS cid
                        FROM messages m
                        JOIN conversations c ON c.id = m.conversation_id
                        WHERE c.session_id = :sid AND c.canal = 'retell_voice'
                          AND m.rol = 'user'
                        """
                    ),
                    {"sid": call_id},
                )
            ).first()
            return row

    row = asyncio.run(_leer())
    assert row is not None, "no se persistió el turno del cliente"
    assert row.claro is None, "la transcripción de la llamada sigue en claro en `contenido`"
    assert row.cifrado is not None, "la transcripción no se ha guardado en el campo cifrado"
    crudo = bytes(row.cifrado)
    assert secreto.encode() not in crudo, "el texto del cliente está legible en la BD"
    assert crudo.startswith(b"gAAAA"), "no parece un token Fernet"
    asyncio.run(_borra_contacto(row.cid))
    assert ConversationCanal.retell_voice.value == "retell_voice"


@pytestmark_db
def test_el_panel_sigue_viendo_lo_que_dijo_el_cliente(monkeypatch):
    """Cifrar no puede significar perder la llamada de vista.

    Todo lo que muestra "el texto del mensaje" ya leía
    `contenido or audio_transcript` (bandeja, ficha del contacto, resumen
    rodante), así que el panel enseña la llamada igual que antes — y el
    serializador de la API expone el campo descifrado.
    """
    from sqlalchemy import select

    import tests.test_voice_gating as vg
    from app.api.conversations import _message_out
    from app.db.session import db_session
    from app.models.conversation import Conversation
    from app.models.message import Message, MessageRole
    from app.services.rolling_summary import _content as _texto_del_resumen

    vg._patch_common(monkeypatch)
    call_id = f"call_{uuid.uuid4().hex[:10]}"
    dicho = "Quiero cita para el martes por la tarde"
    vg._turn(call_id, dicho)

    async def _leer():
        async with db_session() as db:
            conv = (
                await db.execute(
                    select(Conversation).where(Conversation.session_id == call_id)
                )
            ).scalar_one()
            msg = (
                await db.execute(
                    select(Message).where(
                        Message.conversation_id == conv.id, Message.rol == MessageRole.user
                    )
                )
            ).scalar_one()
            return conv.contact_id, msg

    cid, msg = asyncio.run(_leer())
    # El ORM descifra: la app ve el texto de siempre.
    assert msg.audio_transcript == dicho
    # El fallback que usa TODO el panel.
    assert (msg.contenido or msg.audio_transcript) == dicho
    # El resumen rodante (que alimenta el contexto del agente) lo sigue viendo.
    assert _texto_del_resumen(msg) == dicho
    # Y la API lo expone.
    out = _message_out(msg)
    assert out.audio_transcript == dicho
    asyncio.run(_borra_contacto(cid))


@pytestmark_db
def test_la_url_de_la_grabacion_no_se_guarda_en_claro():
    """Mientras `opt_in_signed_url` no esté activo esa URL es PÚBLICA y sin
    caducidad: guardarla en claro era guardar el audio del cliente en claro."""
    from sqlalchemy import select
    from sqlalchemy import text as sql_text

    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal

    call_id = f"call_{uuid.uuid4().hex[:10]}"
    url = f"https://dxc03zgurdly9.cloudfront.net/{uuid.uuid4().hex}/recording.wav"

    async def _flujo():
        async with db_session() as db:
            contact = Contact(
                telefono=f"voice:{call_id}", origen=ContactOrigen.manual, in_crm=False
            )
            db.add(contact)
            await db.flush()
            db.add(
                Conversation(
                    contact_id=contact.id,
                    canal=ConversationCanal.retell_voice,
                    session_id=call_id,
                    call_recording_url=url,
                )
            )
            await db.commit()
            cid = contact.id

        async with db_session() as db:
            crudo = (
                await db.execute(
                    sql_text(
                        "SELECT call_recording_url FROM conversations WHERE session_id = :sid"
                    ),
                    {"sid": call_id},
                )
            ).scalar_one()
            conv = (
                await db.execute(
                    select(Conversation).where(Conversation.session_id == call_id)
                )
            ).scalar_one()
            return cid, bytes(crudo), conv.call_recording_url

    cid, crudo, leido = asyncio.run(_flujo())
    assert url.encode() not in crudo, "la URL de la grabación está legible en la BD"
    assert crudo.startswith(b"gAAAA")
    # El panel la sigue recibiendo entera (el ORM descifra).
    assert leido == url
    asyncio.run(_borra_contacto(cid))


@pytestmark_db
def test_el_filtro_de_metadatos_sigue_funcionando_con_la_url_cifrada():
    """`/voice/status` cuenta llamadas con metadatos con un `IS NOT NULL` sobre
    la URL. Ese predicado tiene que seguir valiendo sobre BYTEA — si no, el
    panel diría "falta el webhook de Retell" con el webhook puesto."""
    from sqlalchemy import func, select

    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal

    call_id = f"call_{uuid.uuid4().hex[:10]}"

    async def _flujo():
        async with db_session() as db:
            contact = Contact(
                telefono=f"voice:{call_id}", origen=ContactOrigen.manual, in_crm=False
            )
            db.add(contact)
            await db.flush()
            db.add(
                Conversation(
                    contact_id=contact.id,
                    canal=ConversationCanal.retell_voice,
                    session_id=call_id,
                    call_recording_url="https://retell.example/r.wav",
                )
            )
            await db.commit()
            cid = contact.id
        async with db_session() as db:
            n = (
                await db.execute(
                    select(func.count())
                    .select_from(Conversation)
                    .where(
                        Conversation.session_id == call_id,
                        Conversation.call_recording_url.isnot(None),
                    )
                )
            ).scalar_one()
            return cid, int(n)

    cid, n = asyncio.run(_flujo())
    assert n == 1
    asyncio.run(_borra_contacto(cid))


def test_contenido_sigue_en_claro_a_proposito():
    """Por qué NO se cifró `messages.contenido` entero (la opción "obvia").

    Hay SQL crudo que lo lee (`api/admin.py`, panel de Atención) y predicados
    SQL sobre su VALOR (`gmail_retention` compara contra ''), y ninguno de los
    dos sobrevive a una columna BYTEA. Este test fija la decisión: si alguien
    cifra la columna, salta aquí antes que en producción.
    """
    from app.models.message import Message

    assert Message.__table__.c.contenido.type.__class__.__name__ == "Text"
    assert Message.__table__.c.audio_transcript.type.__class__.__name__ == "EncryptedText"


# ===========================================================================
# I12 — El borrado RGPD borra la grabación en Retell
# ===========================================================================


def test_el_borrado_rgpd_encuentra_el_helper():
    """`services/data_erasure.py` busca la función por nombre. Comprobamos que
    la que hemos escrito es la que busca — si no, el informe seguiría diciendo
    "grabaciones pendientes" para siempre."""
    from app.providers.voice import retell as retell_mod

    encontrada = next(
        (
            n
            for n in ("delete_call", "delete_call_recording", "erase_call")
            if callable(getattr(retell_mod, n, None))
        ),
        None,
    )
    assert encontrada == "delete_call"


@pytest.mark.asyncio
async def test_el_informe_de_borrado_ya_sale_completo(monkeypatch):
    """Lo que había que comprobar: en cuanto existe la función, el informe deja
    de decir "quedan pendientes" sin tocar nada más."""
    from app.services import data_erasure

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _Db:
        async def execute(self, *a, **k):
            return _Result([("call_aaa", "https://retell/x.wav")])

    llamadas: list[str] = []

    async def _fake_delete(call_id: str) -> bool:
        llamadas.append(call_id)
        return True

    from app.providers.voice import retell as retell_mod

    monkeypatch.setattr(retell_mod, "delete_call", _fake_delete)

    out = await data_erasure._delete_retell_recordings(_Db(), [uuid.uuid4()])
    assert llamadas == ["call_aaa"]
    assert out["pending"] == 0 and out["deleted"] == 1
    assert not out["detail"]


class _FakeResp:
    def __init__(self, status_code):
        self.status_code = status_code


def _patch_creds(monkeypatch, ok=True):
    from app.providers.voice import retell as retell_mod

    async def _creds():
        if not ok:
            return None
        return retell_mod.RetellCredentials(
            api_key="key_test",
            agent_id_retell="agent_1",
            voice_id="v",
            phone_number="+34600000000",
        )

    monkeypatch.setattr(retell_mod, "get_retell_credentials", _creds)

    async def _noop_log(**kw):
        return None

    monkeypatch.setattr(retell_mod, "push_runtime_log", _noop_log)


def _patch_http(monkeypatch, *, status=None, boom=None):
    """Sustituye httpx.AsyncClient por uno que registra la petición."""
    import httpx

    from app.providers.voice import retell as retell_mod

    hecho = {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def delete(self, url, headers=None):
            hecho["method"] = "DELETE"
            hecho["url"] = url
            hecho["headers"] = headers or {}
            if boom:
                raise boom
            return _FakeResp(status)

    monkeypatch.setattr(retell_mod.httpx, "AsyncClient", _Client)
    assert httpx  # el módulo real sigue importado
    return hecho


@pytest.mark.asyncio
async def test_delete_call_usa_el_endpoint_documentado(monkeypatch):
    """DELETE /v2/delete-call/{call_id} con Bearer, 204 sin cuerpo.
    https://docs.retellai.com/api-references/delete-call
    """
    from app.providers.voice.retell import RETELL_API_BASE, delete_call

    _patch_creds(monkeypatch)
    hecho = _patch_http(monkeypatch, status=204)

    assert await delete_call("call_xyz") is True
    assert hecho["method"] == "DELETE"
    assert hecho["url"] == f"{RETELL_API_BASE}/v2/delete-call/call_xyz"
    assert hecho["headers"]["Authorization"] == "Bearer key_test"


@pytest.mark.asyncio
async def test_una_llamada_que_ya_no_existe_cuenta_como_borrada(monkeypatch):
    """404 = no está en Retell. El objetivo del borrado es que no exista, y no
    existe: contarla como pendiente haría que el informe mandase borrar a mano
    algo que ya no está."""
    from app.providers.voice.retell import delete_call

    _patch_creds(monkeypatch)
    _patch_http(monkeypatch, status=404)

    assert await delete_call("call_fantasma") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_un_fallo_de_retell_no_revienta_el_borrado(monkeypatch, status):
    """Tolerante a fallos: devuelve False, no propaga. Un 500 de Retell no
    puede abortar el borrado del contacto, que tiene plazo legal."""
    from app.providers.voice.retell import delete_call

    _patch_creds(monkeypatch)
    _patch_http(monkeypatch, status=status)

    assert await delete_call("call_x") is False


@pytest.mark.asyncio
async def test_la_red_caida_tampoco_revienta_el_borrado(monkeypatch):
    from app.providers.voice.retell import delete_call

    _patch_creds(monkeypatch)
    _patch_http(monkeypatch, boom=RuntimeError("timeout"))

    assert await delete_call("call_x") is False


@pytest.mark.asyncio
async def test_sin_credenciales_no_se_finge_que_se_borro(monkeypatch):
    """Sin API key no se ha borrado nada. Decir True dejaría el audio del
    cliente en Retell con un informe firmado diciendo que no está."""
    from app.providers.voice.retell import delete_call

    _patch_creds(monkeypatch, ok=False)

    assert await delete_call("call_x") is False


@pytest.mark.asyncio
async def test_call_id_vacio_no_llama_a_retell(monkeypatch):
    from app.providers.voice.retell import delete_call

    _patch_creds(monkeypatch)
    hecho = _patch_http(monkeypatch, status=204)

    assert await delete_call("") is False
    assert "url" not in hecho


# ===========================================================================
# Grabaciones firmadas: existe la función, ¿la llama alguien?
# ===========================================================================


@pytestmark_db
def test_borrar_un_contacto_borra_de_verdad_su_grabacion(monkeypatch):
    """La comprobación de que "funciona sin tocar nada más": borrado REAL de un
    contacto con una llamada, con la función REAL del provider. Lo único
    simulado es la petición HTTP a Retell.

    Antes este informe salía con `complete: False` y un aviso de que las
    grabaciones había que borrarlas a mano desde el panel de Retell.
    """
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation, ConversationCanal
    from app.services.data_erasure import erase_contact, finish_erasure

    _patch_creds(monkeypatch)
    hecho = _patch_http(monkeypatch, status=204)
    call_id = f"call_{uuid.uuid4().hex[:10]}"

    async def _flujo():
        async with db_session() as db:
            contact = Contact(
                telefono=f"voice:{call_id}", origen=ContactOrigen.manual, in_crm=False
            )
            db.add(contact)
            await db.flush()
            db.add(
                Conversation(
                    contact_id=contact.id,
                    canal=ConversationCanal.retell_voice,
                    session_id=call_id,
                    call_recording_url="https://retell.example/r.wav",
                )
            )
            await db.commit()
            cid = contact.id

        async with db_session() as db:
            report = await erase_contact(db, cid)
            await db.commit()
        return await finish_erasure(report)

    report = asyncio.run(_flujo())
    assert report["deleted"] is True
    assert report["call_recordings"] == 1
    assert report["call_recordings_deleted"] == 1
    assert report["call_recordings_pending"] == 0
    assert report["complete"] is True, "el informe sigue diciendo que quedan grabaciones"
    assert "warning" not in report
    # Y se llamó al endpoint de Retell de verdad, con el call_id de la llamada.
    assert hecho["url"].endswith(f"/v2/delete-call/{call_id}")


def test_las_grabaciones_firmadas_se_activan_al_guardar_el_canal():
    """`enable_signed_recordings` estaba escrita y no se llamaba desde ningún
    sitio: las grabaciones seguían siendo URLs públicas para siempre.

    El sitio donde tiene que llamarse es al provisionar/guardar el canal Retell
    (`api/admin.py`). Si este test falla, la función sigue muerta.
    """
    from pathlib import Path

    admin = Path(__file__).resolve().parents[1] / "app" / "api" / "admin.py"
    src = admin.read_text(encoding="utf-8")
    assert "enable_signed_recordings" in src, (
        "enable_signed_recordings no se llama en ninguna parte: las grabaciones de "
        "Retell se siguen creando con URL pública y sin caducidad"
    )
