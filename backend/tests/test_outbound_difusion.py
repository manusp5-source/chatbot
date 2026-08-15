"""Difusión de plantillas: lo que hacía que una campaña mintiera o duplicara.

Cada bloque cubre un fallo REAL de la auditoría, no una función bonita:

  1. Sin credenciales la campaña decía "300 enviados" sin mandar nada (el
     provider devolvía el centinela "noop" y quien enviaba lo contaba como
     éxito). Ahora el provider LANZA y el job entero falla con el motivo.
  2. Una clave que no se descifra no es lo mismo que "no configurada".
  3. Con el worker caído la campaña se aceptaba y se quedaba "En cola" para
     siempre — y el rescate de trabajos colgados lo ejecuta ese mismo worker.
  4. Una plantilla RECHAZADA se lanzaba a 300 y salían 300 errores.
  5. Las plantillas con `{{nombre}}` salían como "sin variables" y se enviaban
     con cero parámetros; las que tenían variable en la cabecera contaban una
     de menos.
  6. El listado de plantillas se quedaba en la primera página, y CUALQUIER
     error del proveedor se convertía en lista vacía.
  7. La deduplicación usaba el teléfono en crudo: tres formas de escribir el
     mismo número eran tres destinatarios y la misma persona lo recibía tres
     veces.
  8. Sin clave de idempotencia el proveedor no podía descartar el duplicado de
     un reintento.
  9. La baja vivía en Redis con caducidad: quien la pedía volvía a entrar en la
     campaña de dentro de dos meses.
 10. Si el broker no respondía, el 500 dejaba un trabajo fantasma "En cola".

Patrón DB-gated igual que test_outbound_jobs.py.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest


# ---------------------------------------------------------------------------
# DB-gate
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
# Dobles del proveedor
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict | list, status_code: int = 200, texto: str = "") -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = texto or str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no es JSON")
        return self._payload


class _CapturingClient:
    """httpx.AsyncClient falso que guarda lo que se le manda."""

    def __init__(self, capturado: dict, respuestas: list | None = None) -> None:
        self._cap = capturado
        self._respuestas = respuestas or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def post(self, _url, json=None, headers=None, files=None):
        self._cap["json"] = json
        self._cap["headers"] = headers
        return _FakeResponse({"id": "wamid.1"})

    async def get(self, url, headers=None, params=None):
        self._cap.setdefault("gets", []).append({"url": url, "params": dict(params or {})})
        if self._respuestas:
            return self._respuestas.pop(0)
        return _FakeResponse({"items": []})


def _provider(monkeypatch, *, api_key="k", from_phone="+34900000000", respuestas=None):
    from app.providers.whatsapp import ycloud as mod

    cap: dict = {}
    monkeypatch.setattr(
        mod.httpx, "AsyncClient", lambda **_kw: _CapturingClient(cap, respuestas)
    )
    p = mod.YCloudProvider()

    async def _key():
        return api_key

    async def _from():
        return from_phone

    p._api_key = _key  # type: ignore[method-assign]
    p._from_phone = _from  # type: ignore[method-assign]
    return p, cap


# ---------------------------------------------------------------------------
# 1-2. Credenciales: error explícito, y "no se descifra" ≠ "no configurada"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sin_credenciales_send_template_lanza_en_vez_de_noop(monkeypatch):
    """El fallo original: 300 falsos "enviados" sin llamar a la API ni una vez."""
    from app.providers.whatsapp.ycloud import WhatsAppNotConfiguredError

    p, cap = _provider(monkeypatch, api_key=None)
    with pytest.raises(WhatsAppNotConfiguredError):
        await p.send_template("+34600000000", "recordatorio", "es", ["Ana"])
    assert "json" not in cap, "no debe llegar a llamar al proveedor"


@pytest.mark.asyncio
async def test_sin_numero_de_origen_tambien_lanza(monkeypatch):
    from app.providers.whatsapp.ycloud import WhatsAppNotConfiguredError

    p, _ = _provider(monkeypatch, from_phone="")
    with pytest.raises(WhatsAppNotConfiguredError) as exc:
        await p.send_text("+34600000000", "hola")
    assert "ycloud_phone_number" in str(exc.value)


@pytest.mark.asyncio
async def test_credencial_ilegible_no_se_confunde_con_no_configurada(monkeypatch):
    """Si cambió ENCRYPTION_KEY el arreglo es otro: hay que poder distinguirlo."""
    from app.providers.whatsapp import ycloud as mod
    from app.providers.whatsapp.ycloud import (
        WhatsAppCredentialsUnreadableError,
        WhatsAppNotConfiguredError,
    )
    from app.services.credentials import CredentialUnavailableError

    async def _revienta(key, *, strict=False):
        raise CredentialUnavailableError(f"no se pudo descifrar {key}")

    monkeypatch.setattr(mod, "get_credential", _revienta)
    p = mod.YCloudProvider()
    with pytest.raises(WhatsAppCredentialsUnreadableError):
        await p.check_credentials()
    # Y sigue siendo un "no se puede enviar" para quien solo mira eso.
    assert issubclass(WhatsAppCredentialsUnreadableError, WhatsAppNotConfiguredError)


# ---------------------------------------------------------------------------
# 5. Conteo de variables: cabecera, botones y parámetros CON NOMBRE
# ---------------------------------------------------------------------------


def test_plantilla_con_variables_con_nombre_no_es_sin_variables():
    """`{{nombre}}` es lo que ofrece hoy el asistente de Meta.

    Con el patrón numérico de antes salía como "sin variables" y se enviaba con
    cero parámetros: rechazo en los 300 destinatarios.
    """
    from app.api.admin import _extract_template_parts

    partes = _extract_template_parts(
        {
            "components": [
                {"type": "body", "text": "Hola {{nombre}}, tu cita es el {{fecha}}."}
            ]
        }
    )
    assert partes["variables"] == 2
    assert partes["param_format"] == "named"
    assert partes["variable_names"] == ["nombre", "fecha"]


def test_variables_de_cabecera_y_botones_se_cuentan_aparte():
    """Una variable en la cabecera hacía que la plantilla saliera con "1 var"."""
    from app.api.admin import _extract_template_parts

    partes = _extract_template_parts(
        {
            "components": [
                {"type": "header", "format": "TEXT", "text": "Oferta de {{1}}"},
                {"type": "body", "text": "Hola {{1}}, mira esto."},
                {
                    "type": "buttons",
                    "buttons": [{"type": "URL", "url": "https://x.com/{{1}}"}],
                },
            ]
        }
    )
    assert partes["variables"] == 1
    assert partes["header_variables"] == 1
    assert partes["header_format"] == "text"
    assert partes["button_variables"] == 1


def test_cabecera_con_fichero_se_reconoce():
    from app.api.admin import _extract_template_parts

    partes = _extract_template_parts(
        {"components": [{"type": "HEADER", "format": "IMAGE"}, {"type": "body", "text": "hola"}]}
    )
    assert partes["header_format"] == "image"
    assert partes["header_variables"] == 0


# ---------------------------------------------------------------------------
# 4. Estado de aprobación
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "estado,bloquea",
    [
        ("APPROVED", False),
        ("approved", False),
        ("PENDING", True),
        ("REJECTED", True),
        ("PAUSED", True),
        (None, False),  # el proveedor no informó: no bloqueamos a ciegas
    ],
)
def test_estado_de_plantilla_bloquea_lo_que_debe(estado, bloquea):
    from app.api.admin import _template_status_problem

    assert (_template_status_problem(estado) is not None) is bloquea


# ---------------------------------------------------------------------------
# 6. Listado de plantillas: paginación y errores que NO son lista vacía
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listado_de_plantillas_sigue_el_cursor(monkeypatch):
    """Con más de 100 plantillas, la que buscabas no aparecía y nadie avisaba."""
    pagina1 = _FakeResponse(
        {"items": [{"name": f"t{i}"} for i in range(100)], "nextPageToken": "cur-2"}
    )
    pagina2 = _FakeResponse({"items": [{"name": "la-que-buscaba"}]})
    p, cap = _provider(monkeypatch, respuestas=[_FakeResponse({"items": []}), pagina1, pagina2])

    items = await p.list_templates()

    assert len(items) == 101
    assert items[-1]["name"] == "la-que-buscaba"
    # La segunda página se pidió CON el cursor de la primera.
    assert cap["gets"][-1]["params"].get("pageToken") == "cur-2"


@pytest.mark.asyncio
async def test_listado_de_plantillas_no_gira_sin_fin_con_un_cursor_repetido(monkeypatch):
    """Un proveedor que devuelve siempre el mismo cursor no puede colgarnos."""
    repetida = [
        _FakeResponse({"items": [{"name": "x"}] * 100, "nextPageToken": "igual"})
        for _ in range(30)
    ]
    p, _ = _provider(monkeypatch, respuestas=[_FakeResponse({"items": []}), *repetida])

    items = await p.list_templates()
    assert len(items) == 200  # corta a la segunda vuelta del mismo cursor


@pytest.mark.asyncio
async def test_error_del_proveedor_no_se_disfraza_de_cero_plantillas(monkeypatch):
    """401 y "no tienes plantillas" daban EXACTAMENTE el mismo mensaje en pantalla."""
    from app.providers.whatsapp.ycloud import TemplateListError

    p, _ = _provider(
        monkeypatch,
        respuestas=[_FakeResponse({"items": []}), _FakeResponse({}, 401, "no auth")],
    )
    with pytest.raises(TemplateListError) as exc:
        await p.list_templates()
    assert "401" in str(exc.value)


@pytest.mark.asyncio
async def test_cero_plantillas_sigue_siendo_una_respuesta_valida(monkeypatch):
    p, _ = _provider(
        monkeypatch, respuestas=[_FakeResponse({"items": []}), _FakeResponse({"items": []})]
    )
    assert await p.list_templates() == []


# ---------------------------------------------------------------------------
# 5-6-8. Envío: cabecera, parámetros con nombre e idempotencia
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_template_monta_la_cabecera_con_fichero(monkeypatch):
    p, cap = _provider(monkeypatch)
    await p.send_template(
        "+34600000000",
        "promo",
        "es",
        ["Ana"],
        header={"format": "image", "media_id": "media-1"},
    )
    comps = cap["json"]["template"]["components"]
    assert comps[0]["type"] == "header"
    assert comps[0]["parameters"][0]["image"] == {"id": "media-1"}
    assert comps[1]["type"] == "body"


@pytest.mark.asyncio
async def test_send_template_con_parametros_con_nombre(monkeypatch):
    p, cap = _provider(monkeypatch)
    await p.send_template(
        "+34600000000",
        "promo",
        "es",
        ["Ana", "lunes"],
        param_format="named",
        variable_names=["nombre", "fecha"],
    )
    params = cap["json"]["template"]["components"][0]["parameters"]
    assert params[0] == {"type": "text", "text": "Ana", "parameter_name": "nombre"}
    assert params[1]["parameter_name"] == "fecha"


@pytest.mark.asyncio
async def test_send_template_manda_clave_de_idempotencia(monkeypatch):
    """Para que el proveedor pueda descartar el duplicado de un reintento."""
    p, cap = _provider(monkeypatch)
    await p.send_template("+34600000000", "promo", "es", [], idempotency_key="abc-123")
    assert cap["headers"]["Idempotency-Key"] == "abc-123"


@pytest.mark.asyncio
async def test_cabecera_de_fichero_sin_fichero_es_error_claro(monkeypatch):
    p, _ = _provider(monkeypatch)
    with pytest.raises(ValueError) as exc:
        await p.send_template(
            "+34600000000", "promo", "es", [], header={"format": "image"}
        )
    assert "fichero" in str(exc.value)


def test_clave_de_idempotencia_es_estable_entre_reintentos():
    """Es el id de la fila del destinatario: no cambia al reintentar.

    Si fuera un uuid nuevo por intento no serviría de nada — que es justo el
    caso que había que cubrir: el worker muere entre la llamada y el commit.
    """
    import inspect

    from app.tasks.outbound_send import _process_one

    fuente = inspect.getsource(_process_one)
    assert "idempotency_key=str(recipient.id)" in fuente


# ---------------------------------------------------------------------------
# 9. Opt-out: detección del mensaje de baja
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "texto,es_baja",
    [
        ("BAJA", True),
        ("baja", True),
        ("  Stop  ", True),
        ("STOP.", True),
        ("quiero darme de baja", True),
        ("Unsubscribe", True),
        ("me han dado la baja médica", False),
        ("no me pares el pedido, stop no", False),
        ("hola, quiero información", False),
        ("", False),
        (None, False),
    ],
)
def test_deteccion_de_peticion_de_baja(texto, es_baja):
    from app.models.outbound_optout import is_optout_request

    assert is_optout_request(texto) is es_baja


def test_texto_del_rastro_en_la_conversacion():
    from app.tasks.outbound_send import _campaign_message_text

    assert "recordatorio" in _campaign_message_text("recordatorio", "es", [])
    assert "Ana" in _campaign_message_text("recordatorio", "es", ["Ana"])


# ---------------------------------------------------------------------------
# Helpers DB
# ---------------------------------------------------------------------------


class _FakeWA:
    """Provider falso con la firma NUEVA (cabecera, formato, idempotencia)."""

    def __init__(self, fail_phones: set[str] | None = None, sin_credenciales=False):
        self.sent: list[dict] = []
        self.fail_phones = fail_phones or set()
        self.sin_credenciales = sin_credenciales

    async def check_credentials(self) -> None:
        if self.sin_credenciales:
            from app.providers.whatsapp.ycloud import WhatsAppNotConfiguredError

            raise WhatsAppNotConfiguredError("Falta ycloud_api_key")

    async def send_template(
        self,
        phone,
        template_name,
        language,
        body_variables=None,
        *,
        header=None,
        param_format="positional",
        variable_names=None,
        idempotency_key=None,
    ):
        if phone in self.fail_phones:
            raise RuntimeError("YCloud rechazó el mensaje")
        self.sent.append(
            {
                "phone": phone,
                "variables": list(body_variables or []),
                "header": header,
                "param_format": param_format,
                "idempotency_key": idempotency_key,
            }
        )
        return f"ext-{phone}"


def _patch_provider(monkeypatch, fake) -> None:
    monkeypatch.setattr("app.providers.whatsapp.get_whatsapp_provider", lambda: fake)


def _patch_puertas(monkeypatch, *, worker_ok=True, creds_ok=True, enqueue=None):
    """Neutraliza las comprobaciones previas salvo la que se esté probando."""
    from app.api import admin as admin_mod
    from app.tasks import outbound_send as mod

    async def _worker():
        return None if worker_ok else "El worker no está funcionando"

    async def _creds():
        return None if creds_ok else "Falta ycloud_api_key"

    monkeypatch.setattr(admin_mod, "_worker_problem", _worker)
    monkeypatch.setattr(admin_mod, "_credentials_problem", _creds)
    monkeypatch.setattr(
        mod.send_next_recipient, "delay", enqueue or (lambda *a, **k: None)
    )


async def _seed_user():
    from app.db.session import db_session
    from app.models.user import User, UserRole

    async with db_session() as db:
        user = User(
            email=f"user-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="x",
            role=UserRole.admin,
            nombre="Operador Test",
        )
        db.add(user)
        await db.flush()
        uid = user.id
        await db.commit()
        return uid


async def _get_user(uid):
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.user import User

    async with db_session() as db:
        return (await db.execute(select(User).where(User.id == uid))).scalar_one()


async def _get_job(job_id):
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.outbound_job import OutboundJob

    async with db_session() as db:
        return (
            await db.execute(select(OutboundJob).where(OutboundJob.id == job_id))
        ).scalar_one()


async def _recipients_of(job_id):
    from sqlalchemy import select

    from app.db.session import db_session
    from app.models.outbound_job import OutboundJobRecipient

    async with db_session() as db:
        return (
            await db.execute(
                select(OutboundJobRecipient)
                .where(OutboundJobRecipient.job_id == job_id)
                .order_by(OutboundJobRecipient.position)
            )
        ).scalars().all()


def _telefonos_de_este_test(cuantos: int) -> list[dict]:
    """Destinatarios distintos en cada ejecución.

    La base de pruebas no se vacía entre ejecuciones, y el alta de difusiones
    rechaza el MISMO envío a los MISMOS destinatarios mientras siga en marcha
    (freno al doble clic). Con números fijos, la segunda vez que se corre la
    suite en cinco minutos el test chocaba consigo mismo.
    """
    base = uuid.uuid4().int % 1_000
    return [{"phone": f"+346{base:03d}{i:04d}"} for i in range(cuantos)]


def _body(**kw):
    from app.api.admin import OutboundJobCreate, OutboundJobRecipientIn

    recipients = [OutboundJobRecipientIn(**r) for r in kw.pop("recipients")]
    return OutboundJobCreate(recipients=recipients, **kw)


# ---------------------------------------------------------------------------
# 7. Deduplicación con el teléfono NORMALIZADO
# ---------------------------------------------------------------------------


@pytestmark_db
@pytest.mark.asyncio
async def test_tres_formas_del_mismo_numero_son_un_solo_destinatario(monkeypatch):
    """El CRM admite las tres (el alta manual no normaliza) y el envío
    normalizaba DESPUÉS: la misma persona recibía el mensaje tres veces."""
    from app.api.admin import create_outbound_job
    from app.db.session import db_session

    _patch_provider(monkeypatch, _FakeWA())
    _patch_puertas(monkeypatch)
    user = await _get_user(await _seed_user())
    sufijo = f"{uuid.uuid4().int % 10**9:09d}"

    body = _body(
        template_name="recordatorio",
        language="es",
        recipients=[
            {"phone": f"+34{sufijo}", "variables": []},
            {"phone": f"34{sufijo}", "variables": []},
            {"phone": f" 34{sufijo} ", "variables": []},
        ],
    )
    async with db_session() as db:
        out = await create_outbound_job(body, db=db, user=user)

    assert out.total == 1
    assert (await _recipients_of(out.id))[0].phone == f"+34{sufijo}"


@pytestmark_db
@pytest.mark.asyncio
async def test_el_identificador_wa_sobrevive_a_la_normalizacion(monkeypatch):
    """Un `wa:<bsuid>` no es un teléfono: no puede acabar como "+wa:ES.123"."""
    from app.api.admin import create_outbound_job
    from app.db.session import db_session

    _patch_provider(monkeypatch, _FakeWA())
    _patch_puertas(monkeypatch)
    user = await _get_user(await _seed_user())
    ident = f"wa:ES.{uuid.uuid4().hex}"

    async with db_session() as db:
        out = await create_outbound_job(
            _body(template_name="t", language="es", recipients=[{"phone": ident}]),
            db=db,
            user=user,
        )
    assert (await _recipients_of(out.id))[0].phone == ident


# ---------------------------------------------------------------------------
# 3. Worker caído y 1. credenciales: se comprueban ANTES de aceptar
# ---------------------------------------------------------------------------


@pytestmark_db
@pytest.mark.asyncio
async def test_no_se_acepta_la_campana_con_el_worker_caido(monkeypatch):
    """Es el fallo típico de quien no levantó el contenedor del worker: la
    campaña se quedaba «En cola» para siempre y el rescate de trabajos colgados
    lo ejecuta ese mismo worker."""
    from fastapi import HTTPException

    from app.api.admin import create_outbound_job
    from app.db.session import db_session
    from app.models.outbound_job import OutboundJob

    _patch_provider(monkeypatch, _FakeWA())
    _patch_puertas(monkeypatch, worker_ok=False)
    user = await _get_user(await _seed_user())

    async with db_session() as db:
        antes = await _contar_jobs(db)
        with pytest.raises(HTTPException) as exc:
            await create_outbound_job(
                _body(template_name="t", language="es", recipients=[{"phone": "+34600000001"}]),
                db=db,
                user=user,
            )
        assert exc.value.status_code == 503
        assert "worker" in str(exc.value.detail).lower()
    async with db_session() as db:
        assert await _contar_jobs(db) == antes, "no puede quedar ningún job creado"
    assert OutboundJob is not None


async def _contar_jobs(db) -> int:
    from sqlalchemy import func, select

    from app.models.outbound_job import OutboundJob

    return (await db.execute(select(func.count()).select_from(OutboundJob))).scalar_one()


@pytestmark_db
@pytest.mark.asyncio
async def test_no_se_acepta_la_campana_sin_credenciales(monkeypatch):
    from fastapi import HTTPException

    from app.api.admin import create_outbound_job
    from app.db.session import db_session

    _patch_provider(monkeypatch, _FakeWA())
    _patch_puertas(monkeypatch, creds_ok=False)
    user = await _get_user(await _seed_user())

    async with db_session() as db:
        with pytest.raises(HTTPException) as exc:
            await create_outbound_job(
                _body(template_name="t", language="es", recipients=[{"phone": "+34600000002"}]),
                db=db,
                user=user,
            )
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# 10. Broker caído: nada de trabajos fantasma
# ---------------------------------------------------------------------------


@pytestmark_db
@pytest.mark.asyncio
async def test_broker_caido_no_deja_trabajo_fantasma(monkeypatch):
    """Antes: commit y DESPUÉS encolar. El operador veía "no se pudo crear el
    envío" y el trabajo existía igualmente, en «En cola» para siempre."""
    from fastapi import HTTPException

    from app.api.admin import create_outbound_job
    from app.db.session import db_session

    def _revienta(*_a, **_kw):
        raise ConnectionError("Redis no responde")

    _patch_provider(monkeypatch, _FakeWA())
    _patch_puertas(monkeypatch, enqueue=_revienta)
    user = await _get_user(await _seed_user())

    async with db_session() as db:
        antes = await _contar_jobs(db)
        with pytest.raises(HTTPException) as exc:
            await create_outbound_job(
                _body(template_name="t", language="es", recipients=[{"phone": "+34600000003"}]),
                db=db,
                user=user,
            )
        assert exc.value.status_code == 503
    async with db_session() as db:
        assert await _contar_jobs(db) == antes


# ---------------------------------------------------------------------------
# 1. El worker no cuenta como enviado lo que no se envió
# ---------------------------------------------------------------------------


@pytestmark_db
@pytest.mark.asyncio
async def test_sin_credenciales_el_job_falla_y_no_cuenta_ni_un_envio(monkeypatch):
    from app.api.admin import create_outbound_job
    from app.db.session import db_session
    from app.tasks.outbound_send import _process_one

    _patch_provider(monkeypatch, _FakeWA())
    _patch_puertas(monkeypatch)
    user = await _get_user(await _seed_user())
    async with db_session() as db:
        out = await create_outbound_job(
            _body(
                template_name="t",
                language="es",
                throttle_min_seconds=0,
                throttle_max_seconds=0,
                recipients=[{"phone": f"+3460000{i:04d}"} for i in range(3)],
            ),
            db=db,
            user=user,
        )

    # Ahora el proveedor se queda sin credenciales, como en producción.
    sin_creds = _FakeWA(sin_credenciales=True)
    _patch_provider(monkeypatch, sin_creds)
    assert await _process_one(out.id) is None

    job = await _get_job(out.id)
    assert job.status == "failed"
    assert job.sent_ok == 0, "ni un solo falso 'enviado'"
    assert "ycloud_api_key" in (job.error or "")
    assert sin_creds.sent == []


# ---------------------------------------------------------------------------
# 9. Opt-out persistente, respetado en la creación y en el envío
# ---------------------------------------------------------------------------


@pytestmark_db
@pytest.mark.asyncio
async def test_quien_pidio_la_baja_no_entra_en_la_campana(monkeypatch):
    from app.api.admin import create_outbound_job
    from app.db.session import db_session
    from app.models.outbound_optout import OPTOUT_SOURCE_REPLY, add_outbound_optout

    _patch_provider(monkeypatch, _FakeWA())
    _patch_puertas(monkeypatch)
    user = await _get_user(await _seed_user())
    de_baja = f"+34{uuid.uuid4().int % 10**9:09d}"
    normal = f"+34{uuid.uuid4().int % 10**9:09d}"

    async with db_session() as db:
        # De baja escrita SIN el "+": tiene que casar igual.
        await add_outbound_optout(db, de_baja[1:], source=OPTOUT_SOURCE_REPLY)
        await db.commit()

    async with db_session() as db:
        out = await create_outbound_job(
            _body(
                template_name="t",
                language="es",
                recipients=[{"phone": de_baja}, {"phone": normal}],
            ),
            db=db,
            user=user,
        )
    assert out.total == 1
    assert [r.phone for r in await _recipients_of(out.id)] == [normal]


@pytestmark_db
@pytest.mark.asyncio
async def test_la_baja_es_idempotente():
    from app.db.session import db_session
    from app.models.outbound_optout import add_outbound_optout

    phone = f"+34{uuid.uuid4().int % 10**9:09d}"
    async with db_session() as db:
        assert await add_outbound_optout(db, phone) is True
        await db.commit()
    async with db_session() as db:
        assert await add_outbound_optout(db, phone) is False
        await db.commit()


@pytestmark_db
@pytest.mark.asyncio
async def test_el_envio_tambien_respeta_la_baja(monkeypatch):
    """La comprobación del worker es la que manda: alguien puede darse de baja
    con la campaña ya lanzada."""
    from app.api.admin import create_outbound_job
    from app.db.session import db_session
    from app.models.outbound_optout import add_outbound_optout
    from app.tasks.outbound_send import _process_one

    fake = _FakeWA()
    _patch_provider(monkeypatch, fake)
    _patch_puertas(monkeypatch)
    user = await _get_user(await _seed_user())
    phone = f"+34{uuid.uuid4().int % 10**9:09d}"

    async with db_session() as db:
        out = await create_outbound_job(
            _body(
                template_name="t",
                language="es",
                throttle_min_seconds=0,
                throttle_max_seconds=0,
                recipients=[{"phone": phone}],
            ),
            db=db,
            user=user,
        )
    # Se da de baja DESPUÉS de crearse la campaña.
    async with db_session() as db:
        await add_outbound_optout(db, phone)
        await db.commit()

    await _process_one(out.id)

    assert fake.sent == []
    job = await _get_job(out.id)
    assert job.sent_ok == 0 and job.sent_error == 1
    assert "baja" in ((await _recipients_of(out.id))[0].error or "").lower()


# ---------------------------------------------------------------------------
# 8. La clave de idempotencia llega al proveedor en el envío real
# ---------------------------------------------------------------------------


@pytestmark_db
@pytest.mark.asyncio
async def test_el_worker_pasa_la_clave_de_idempotencia(monkeypatch):
    from app.api.admin import create_outbound_job
    from app.db.session import db_session
    from app.tasks.outbound_send import _process_one

    fake = _FakeWA()
    _patch_provider(monkeypatch, fake)
    _patch_puertas(monkeypatch)
    user = await _get_user(await _seed_user())

    async with db_session() as db:
        out = await create_outbound_job(
            _body(
                template_name="t",
                language="es",
                throttle_min_seconds=0,
                throttle_max_seconds=0,
                recipients=[{"phone": f"+34{uuid.uuid4().int % 10**9:09d}"}],
            ),
            db=db,
            user=user,
        )
    await _process_one(out.id)

    recipient = (await _recipients_of(out.id))[0]
    assert fake.sent[0]["idempotency_key"] == str(recipient.id)


# ---------------------------------------------------------------------------
# 11. Reintento de los fallidos + recorte anunciado
# ---------------------------------------------------------------------------


@pytestmark_db
@pytest.mark.asyncio
async def test_reintentar_crea_una_campana_nueva_solo_con_los_fallidos(monkeypatch):
    from app.api.admin import create_outbound_job, retry_outbound_job
    from app.db.session import db_session
    from app.tasks.outbound_send import _process_one

    phones = [f"+34{uuid.uuid4().int % 10**9:09d}" for _ in range(3)]
    fake = _FakeWA(fail_phones={phones[1]})
    _patch_provider(monkeypatch, fake)
    _patch_puertas(monkeypatch)
    user = await _get_user(await _seed_user())

    async with db_session() as db:
        out = await create_outbound_job(
            _body(
                template_name="t",
                language="es",
                throttle_min_seconds=0,
                throttle_max_seconds=0,
                recipients=[{"phone": p} for p in phones],
            ),
            db=db,
            user=user,
        )
    for _ in range(4):
        if await _process_one(out.id) is None:
            break

    async with db_session() as db:
        nuevo = await retry_outbound_job(out.id, db=db, user=user)

    assert nuevo.id != out.id
    assert nuevo.total == 1
    assert [r.phone for r in await _recipients_of(nuevo.id)] == [phones[1]]


@pytestmark_db
@pytest.mark.asyncio
async def test_el_detalle_del_job_dice_cuantos_faltan_y_si_recorta(monkeypatch):
    from app.api.admin import create_outbound_job, get_outbound_job
    from app.db.session import db_session

    _patch_provider(monkeypatch, _FakeWA())
    _patch_puertas(monkeypatch)
    user = await _get_user(await _seed_user())

    # Números distintos en cada ejecución: el alta rechaza con un 409 el mismo
    # envío a los mismos destinatarios si aún hay uno igual en marcha (freno al
    # doble clic), y la base de datos de pruebas no se vacía entre ejecuciones.
    async with db_session() as db:
        out = await create_outbound_job(
            _body(
                template_name="t",
                language="es",
                recipients=_telefonos_de_este_test(5),
            ),
            db=db,
            user=user,
        )
    async with db_session() as db:
        detalle = await get_outbound_job(out.id, db=db)

    assert detalle.pending == 5
    assert detalle.errors_total == 0
    assert detalle.errors_truncated is False


# ---------------------------------------------------------------------------
# 12. Rastro de la campaña en la conversación del contacto
# ---------------------------------------------------------------------------


@pytestmark_db
@pytest.mark.asyncio
async def test_la_campana_deja_rastro_en_la_conversacion(monkeypatch):
    """Cuando el cliente contesta "sí, me interesa", el hilo tiene que empezar
    por lo que se le mandó, no por su respuesta."""
    from sqlalchemy import select

    from app.api.admin import create_outbound_job
    from app.db.session import db_session
    from app.models.contact import Contact, ContactOrigen
    from app.models.conversation import Conversation
    from app.models.message import Message
    from app.tasks.outbound_send import _process_one

    _patch_provider(monkeypatch, _FakeWA())
    _patch_puertas(monkeypatch)
    user = await _get_user(await _seed_user())
    phone = f"+34{uuid.uuid4().int % 10**9:09d}"

    async with db_session() as db:
        db.add(Contact(telefono=phone, nombre="Ana", origen=ContactOrigen.whatsapp))
        await db.commit()

    async with db_session() as db:
        out = await create_outbound_job(
            _body(
                template_name="recordatorio",
                language="es",
                throttle_min_seconds=0,
                throttle_max_seconds=0,
                recipients=[{"phone": phone, "variables": ["Ana"]}],
            ),
            db=db,
            user=user,
        )
    await _process_one(out.id)

    try:
        async with db_session() as db:
            contacto = (
                await db.execute(select(Contact).where(Contact.telefono == phone))
            ).scalar_one()
            conv = (
                await db.execute(
                    select(Conversation).where(Conversation.contact_id == contacto.id)
                )
            ).scalar_one()
            msgs = (
                await db.execute(
                    select(Message).where(Message.conversation_id == conv.id)
                )
            ).scalars().all()

        assert len(msgs) == 1
        assert "recordatorio" in (msgs[0].contenido or "")
        assert msgs[0].extra["source"] == "outbound_campaign"
        assert msgs[0].extra["outbound_job_id"] == str(out.id)
    finally:
        # Limpieza OBLIGATORIA: la BD de tests es compartida y este test es el
        # único que crea conversaciones. Dejarlas ahí va llenando la bandeja de
        # whatsapp/bot y tumba a otro test que lista las 50 primeras.
        async with db_session() as db:
            c = (
                await db.execute(select(Contact).where(Contact.telefono == phone))
            ).scalar_one_or_none()
            if c is not None:
                await db.delete(c)  # cascada: conversación y mensajes
                await db.commit()


@pytestmark_db
@pytest.mark.asyncio
async def test_el_doble_clic_no_manda_la_campana_dos_veces(monkeypatch):
    """Cada difusión repetida es una conversación de WhatsApp facturada POR
    PERSONA. Con 300 destinatarios eso es dinero y es quemar la reputación del
    número. La deduplicación que había solo actuaba dentro de una petición."""
    from fastapi import HTTPException

    from app.api.admin import create_outbound_job
    from app.db.session import db_session

    _patch_provider(monkeypatch, _FakeWA())
    _patch_puertas(monkeypatch)
    user = await _get_user(await _seed_user())
    destinatarios = _telefonos_de_este_test(3)

    async with db_session() as db:
        await create_outbound_job(
            _body(template_name="doble", language="es", recipients=destinatarios),
            db=db,
            user=user,
        )
    async with db_session() as db:
        with pytest.raises(HTTPException) as exc:
            await create_outbound_job(
                _body(template_name="doble", language="es", recipients=destinatarios),
                db=db,
                user=user,
            )
    assert exc.value.status_code == 409
    assert "Envíos recientes" in str(exc.value.detail)


@pytestmark_db
@pytest.mark.asyncio
async def test_otra_audiencia_con_la_misma_plantilla_sigue_pudiendose_enviar(monkeypatch):
    """El freno no puede estorbar a un envío legítimo."""
    from app.api.admin import create_outbound_job
    from app.db.session import db_session

    _patch_provider(monkeypatch, _FakeWA())
    _patch_puertas(monkeypatch)
    user = await _get_user(await _seed_user())

    async with db_session() as db:
        await create_outbound_job(
            _body(
                template_name="misma",
                language="es",
                recipients=_telefonos_de_este_test(3),
            ),
            db=db,
            user=user,
        )
    async with db_session() as db:
        segundo = await create_outbound_job(
            _body(
                template_name="misma",
                language="es",
                recipients=_telefonos_de_este_test(3),
            ),
            db=db,
            user=user,
        )
    assert segundo.total == 3
