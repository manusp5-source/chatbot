"""Envío masivo de plantillas WhatsApp "poco a poco", en segundo plano.

La tarea procesa UN destinatario por invocación y, si quedan más y el job no
está cancelado, se RE-ENCOLA a sí misma con `apply_async(countdown=...)` usando
un retardo aleatorio dentro de la ventana [throttle_min, throttle_max] del job.

Por qué re-encolar en vez de un único task que duerme entre envíos: los workers
Celery también procesan los mensajes ENTRANTES del chatbot (con prefetch=1). Un
task que hiciera `sleep(20-40s)` por cada uno de cientos de destinatarios
ocuparía un worker durante horas y degradaría el chatbot. Re-encolar con
`countdown` libera el worker entre envíos; el "poco a poco" lo da el countdown.

Seguridad ante cancelación concurrente: NUNCA mutamos `outbound_job.status`
sobre el objeto ORM cargado (eso podría pisar un cancel que ocurrió mientras
enviábamos). Todas las transiciones del job se hacen con `UPDATE ... WHERE`
guardados, y el cancel se re-comprueba leyendo el estado fresco antes de
re-encolar.

Ningún job puede quedarse "en curso" para siempre (antes pasaba: si el trabajo
moría, nadie se enteraba). Dos redes:
  - reintentos de Celery agotados → el job pasa a `failed` con el motivo
    guardado y un log de error;
  - `reap_stale_jobs` (beat, cada 15 min) cierra los `running` que llevan mucho
    sin mover un destinatario — el caso de que se pierda el mensaje de
    re-encolado (Redis reiniciado) y no quede nadie que lo retome.
"""
import asyncio
import contextlib
import random
import uuid
from datetime import datetime, timedelta, timezone

from celery import shared_task
from celery.exceptions import MaxRetriesExceededError
from sqlalchemy import func, select, update

from app.core.logging import get_logger
from app.db.session import db_session

logger = get_logger(__name__)

# Margen mínimo antes de dar por muerto un job `running` (30 min). Un job vivo
# toca la BD como mucho cada `throttle_max` segundos; con ritmos lentos el
# margen escala con el propio ritmo (ver stale_after_seconds).
STALE_MIN_SECONDS = 1800
STALE_THROTTLE_FACTOR = 3


@shared_task(name="app.tasks.outbound_send.send_next_recipient", bind=True, max_retries=3)
def send_next_recipient(self, job_id: str) -> None:
    """Envía el siguiente destinatario pendiente del job y se re-encola.

    Devuelve None siempre; el control del ritmo está en el `countdown` del
    re-encolado.
    """
    try:
        delay = asyncio.run(_process_one(uuid.UUID(job_id)))
    except Exception as exc:
        retries = self.request.retries or 0
        max_retries = self.max_retries or 0
        logger.error(
            "outbound_send.error", job_id=job_id, error=str(exc), retries=retries
        )
        if retries < max_retries:
            try:
                raise self.retry(exc=exc, countdown=2**retries * 5)
            except MaxRetriesExceededError:
                pass  # cae al cierre terminal de abajo
        # Sin reintentos por delante: el job NO puede quedarse `running` para
        # siempre. Estado terminal + motivo (lo que ve la operadora en la UI)
        # + log de error, que es de lo que se entera la alerta.
        _fail_job_sync(
            job_id,
            f"El envío se detuvo tras {retries} reintentos: {str(exc)[:400]}",
        )
        return

    if delay is not None:
        send_next_recipient.apply_async(args=[job_id], countdown=delay)


async def _process_one(job_id: uuid.UUID) -> float | None:
    """Procesa un destinatario. Devuelve el countdown (s) para re-encolar, o
    None si el job ha terminado, fallado o ha sido cancelado (no re-encolar)."""
    from app.models.outbound_job import (
        JOB_STATUS_CANCELED,
        JOB_STATUS_DONE,
        JOB_STATUS_FAILED,
        JOB_STATUS_QUEUED,
        JOB_STATUS_RUNNING,
        RECIPIENT_STATUS_ERROR,
        RECIPIENT_STATUS_OK,
        RECIPIENT_STATUS_PENDING,
        OutboundJob,
        OutboundJobRecipient,
    )
    from app.providers.whatsapp import get_whatsapp_provider
    from app.providers.whatsapp.ycloud import WhatsAppNotConfiguredError

    async with db_session() as db:
        job = (
            await db.execute(select(OutboundJob).where(OutboundJob.id == job_id))
        ).scalar_one_or_none()
        if job is None:
            return None
        if job.status in (JOB_STATUS_CANCELED, JOB_STATUS_DONE, JOB_STATUS_FAILED):
            return None

        # Datos inmutables del job que necesitamos durante el envío.
        template_name = job.template_name
        language = job.language
        throttle_min = job.throttle_min_seconds
        throttle_max = job.throttle_max_seconds
        # `getattr` con defecto: estos tres son columnas nuevas y la tarea tiene
        # que seguir procesando un job cargado por un camino que no las traiga.
        header = getattr(job, "header", None)
        param_format = getattr(job, "param_format", None) or "positional"
        variable_names = getattr(job, "variable_names", None) or None

        # queued → running (guardado: solo si sigue en queued).
        if job.status == JOB_STATUS_QUEUED:
            await db.execute(
                update(OutboundJob)
                .where(OutboundJob.id == job_id, OutboundJob.status == JOB_STATUS_QUEUED)
                .values(status=JOB_STATUS_RUNNING, started_at=_now())
            )

        # Siguiente pendiente, en orden. `FOR UPDATE SKIP LOCKED`: si por lo que
        # sea hay dos vueltas del job solapadas (un reintento que pisa al
        # re-encolado), cada una se lleva un destinatario distinto en vez de
        # pelearse por el mismo y mandarlo dos veces.
        recipient = (
            await db.execute(
                select(OutboundJobRecipient)
                .where(
                    OutboundJobRecipient.job_id == job_id,
                    OutboundJobRecipient.status == RECIPIENT_STATUS_PENDING,
                )
                .order_by(OutboundJobRecipient.position)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()

        if recipient is None:
            # No quedan pendientes → done (sin pisar un cancel).
            await db.execute(
                update(OutboundJob)
                .where(OutboundJob.id == job_id, OutboundJob.status == JOB_STATUS_RUNNING)
                .values(status=JOB_STATUS_DONE, finished_at=_now())
            )
            await db.commit()
            return None

        wa = get_whatsapp_provider()
        if not hasattr(wa, "send_template"):
            await db.execute(
                update(OutboundJob)
                .where(OutboundJob.id == job_id)
                .values(
                    status=JOB_STATUS_FAILED,
                    error="El provider WhatsApp actual no soporta plantillas",
                    finished_at=_now(),
                )
            )
            await db.commit()
            return None

        # Sin credenciales no se manda NADA: el job entero falla aquí, con el
        # motivo, en vez de recorrer 300 destinatarios marcando 300 errores
        # idénticos (o, como antes, contando 300 falsos "enviados" porque el
        # provider devolvía el centinela "noop" sin llamar a la API).
        check = getattr(wa, "check_credentials", None)
        if check is not None:
            try:
                await check()
            except WhatsAppNotConfiguredError as exc:
                await db.execute(
                    update(OutboundJob)
                    .where(OutboundJob.id == job_id)
                    .values(
                        status=JOB_STATUS_FAILED,
                        error=str(exc)[:1000],
                        finished_at=_now(),
                    )
                )
                await db.commit()
                logger.error(
                    "outbound_send.no_credentials", job_id=str(job_id), error=str(exc)
                )
                return None

        # Bajas y bloqueos: NO enviamos. Se registra como error visible en el
        # progreso, no detiene el job.
        #  - opt-out: la baja PERMANENTE que pidió la persona (tabla, sin TTL).
        #  - blocklist: cortes temporales por abuso (Redis, con TTL).
        # Son dos cosas distintas y se comprueban las dos.
        from app.services.agent_guardrails import is_phone_blocked

        # Datos para el rastro en la conversación. Se anota DESPUÉS del commit
        # del destinatario: entre el envío y su commit no puede colarse ninguna
        # otra operación (ver el comentario del commit corto más abajo).
        trace: tuple[str, str] | None = None

        block_reason = await _blocked_reason(db, recipient.phone, is_phone_blocked)
        if block_reason:
            recipient.status = RECIPIENT_STATUS_ERROR
            recipient.error = block_reason
            await db.execute(
                update(OutboundJob)
                .where(OutboundJob.id == job_id)
                .values(sent_error=OutboundJob.sent_error + 1)
            )
        else:
            # Envío del destinatario. Un fallo del envío se REGISTRA, no detiene
            # el job (el operador verá los errores en el progreso).
            try:
                ext = await _send_one(
                    wa,
                    phone=recipient.phone,
                    template_name=template_name,
                    language=language,
                    variables=recipient.variables or [],
                    header=header,
                    param_format=param_format,
                    variable_names=variable_names,
                    # Clave de idempotencia ESTABLE: el id de la fila del
                    # destinatario. Si el worker muere entre la llamada al
                    # proveedor y el commit, el destinatario vuelve a
                    # `pending` y se reintenta — con la misma clave, así que
                    # el proveedor puede descartar el duplicado en vez de
                    # mandarle el mensaje dos veces.
                    idempotency_key=str(recipient.id),
                )
                recipient.status = RECIPIENT_STATUS_OK
                recipient.external_id = ext
                await db.execute(
                    update(OutboundJob)
                    .where(OutboundJob.id == job_id)
                    .values(sent_ok=OutboundJob.sent_ok + 1)
                )
                trace = (recipient.phone, ext)
            except Exception as exc:
                recipient.status = RECIPIENT_STATUS_ERROR
                recipient.error = str(exc)[:1000]
                await db.execute(
                    update(OutboundJob)
                    .where(OutboundJob.id == job_id)
                    .values(sent_error=OutboundJob.sent_error + 1)
                )
        recipient.sent_at = _now()

        # COMMIT CORTO, pegado al envío. El mensaje ya salió hacia el cliente:
        # el estado del destinatario tiene que quedar en firme AHORA. Si lo
        # dejáramos para el final de la función, cualquier fallo intermedio
        # (BD, red, el propio worker muriendo) revertía la transacción, el
        # destinatario volvía a `pending` y el reintento de Celery le mandaba
        # la plantilla otra vez. Al estar ya marcado ok/error, el reintento
        # simplemente lo salta: el SELECT de arriba solo mira los `pending`.
        await db.commit()

        # Rastro en la conversación del contacto: si el cliente contesta "sí,
        # me interesa", el hilo tiene que empezar por lo que se le mandó, no por
        # su respuesta. Va DESPUÉS del commit del destinatario (nada puede
        # meterse entre el envío y ese commit) y no propaga errores: el mensaje
        # al cliente ya salió, perder la anotación no lo convierte en un fallo.
        if trace is not None:
            await _log_campaign_message(
                db,
                phone=trace[0],
                template_name=template_name,
                language=language,
                variables=recipient.variables or [],
                external_id=trace[1],
                job_id=job_id,
            )

        # ¿Quedan más pendientes?
        remaining = (
            await db.execute(
                select(func.count())
                .select_from(OutboundJobRecipient)
                .where(
                    OutboundJobRecipient.job_id == job_id,
                    OutboundJobRecipient.status == RECIPIENT_STATUS_PENDING,
                )
            )
        ).scalar_one()

        if remaining == 0:
            await db.execute(
                update(OutboundJob)
                .where(OutboundJob.id == job_id, OutboundJob.status == JOB_STATUS_RUNNING)
                .values(status=JOB_STATUS_DONE, finished_at=_now())
            )
            await db.commit()
            return None

        await db.commit()

        # Re-comprobar cancelación con estado fresco (otra transacción pudo
        # cancelar mientras enviábamos): si está cancelado, no re-encolamos.
        status_now = (
            await db.execute(select(OutboundJob.status).where(OutboundJob.id == job_id))
        ).scalar_one_or_none()
        if status_now == JOB_STATUS_CANCELED:
            return None

        lo = max(0, int(throttle_min))
        hi = max(lo, int(throttle_max))
        return random.uniform(lo, hi)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _blocked_reason(db, phone: str, is_phone_blocked) -> str | None:
    """Motivo por el que NO se envía a este destinatario, o None.

    Dos listas distintas: la baja PERMANENTE que pidió la persona (tabla
    `outbound_optout`, sin caducidad) y la blocklist de abuso (Redis, con TTL).
    Antes solo se miraba la segunda, así que una baja "se curaba" sola a los 30
    días y la persona volvía a recibir difusiones.
    """
    from sqlalchemy import select as sa_select

    from app.models.outbound_optout import OutboundOptOut

    opted = (
        await db.execute(
            sa_select(OutboundOptOut.id).where(OutboundOptOut.phone == phone)
        )
    ).scalar_one_or_none()
    if opted is not None:
        return "Dado de baja de las difusiones; no se envió"
    if await is_phone_blocked(phone):
        return "Destinatario en la blocklist; no se envió"
    return None


async def _send_one(
    wa,
    *,
    phone: str,
    template_name: str,
    language: str,
    variables: list[str],
    header: dict | None,
    param_format: str,
    variable_names: list[str] | None,
    idempotency_key: str,
) -> str:
    """Llama a `send_template` pasando solo lo que ese provider acepta.

    Los parámetros nuevos (cabecera, formato con nombre, clave de idempotencia)
    son keyword-only: un provider antiguo —o un doble de test— que solo declare
    los cuatro de siempre sigue funcionando.
    """
    import inspect

    extra = {
        "header": header,
        "param_format": param_format,
        "variable_names": variable_names,
        "idempotency_key": idempotency_key,
    }
    try:
        acepta = set(inspect.signature(wa.send_template).parameters)
    except (TypeError, ValueError):  # pragma: no cover — provider exótico
        acepta = set()
    kwargs = {k: v for k, v in extra.items() if k in acepta}
    return await wa.send_template(phone, template_name, language, variables, **kwargs)


async def _log_campaign_message(
    db,
    *,
    phone: str,
    template_name: str,
    language: str,
    variables: list[str],
    external_id: str | None,
    job_id: uuid.UUID,
) -> None:
    """Deja el mensaje de la campaña en la conversación del contacto.

    Sin esto, cuando el cliente contestaba "sí, me interesa" ni el agente ni la
    operadora veían QUÉ se le había mandado: el hilo empezaba por la respuesta.

    Best-effort de verdad: el mensaje al cliente YA salió, así que un fallo aquí
    se registra y se sigue. Nunca convierte un envío bueno en un error.
    """
    from sqlalchemy import select as sa_select

    from app.models.contact import Contact
    from app.models.conversation import (
        Conversation,
        ConversationCanal,
        ConversationStatus,
    )
    from app.models.message import Message, MessageRole
    from app.services.delivery_status import marcar_enviado

    try:
        contact = (
            await db.execute(sa_select(Contact).where(Contact.telefono == phone))
        ).scalar_one_or_none()
        if contact is None:
            return  # sin ficha en el CRM no hay hilo donde anotarlo

        conv = (
            await db.execute(
                sa_select(Conversation)
                .where(
                    Conversation.contact_id == contact.id,
                    Conversation.canal == ConversationCanal.whatsapp,
                    Conversation.ended_at.is_(None),
                )
                .order_by(Conversation.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if conv is None:
            conv = Conversation(
                contact_id=contact.id,
                canal=ConversationCanal.whatsapp,
                session_id=phone,
                status=ConversationStatus.bot,
            )
            db.add(conv)
            await db.flush()

        db.add(
            Message(
                conversation_id=conv.id,
                rol=MessageRole.operator,
                contenido=_campaign_message_text(template_name, language, variables),
                # Una difusión es justo donde más duele no saber quién no la ha
                # recibido: se manda a cientos y los números muertos o los que
                # bloquearon a la empresa fallan de uno en uno, en silencio.
                extra=marcar_enviado(
                    {
                        "source": "outbound_campaign",
                        "outbound_job_id": str(job_id),
                        "template_name": template_name,
                        "language": language,
                    },
                    external_id or "",
                    ConversationCanal.whatsapp,
                ),
            )
        )
        conv.last_message_at = _now()
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        # El commit del destinatario ya está hecho, así que revertir aquí solo
        # tira la anotación. Sin este rollback la sesión se quedaría sucia y el
        # siguiente commit de la tarea reventaría por culpa de un apunte.
        with contextlib.suppress(Exception):
            await db.rollback()
        logger.error(
            "outbound_send.conversation_trace_failed",
            job_id=str(job_id),
            error=str(exc)[:300],
        )


def _campaign_message_text(
    template_name: str, language: str, variables: list[str]
) -> str:
    """Texto que se guarda en el hilo. Función pura.

    No tenemos el cuerpo de la plantilla en el worker (vive en el proveedor),
    así que guardamos el nombre y los valores con los que se envió: es lo que
    necesita quien lee el hilo para saber qué recibió esa persona.
    """
    linea = f"[Campaña] Plantilla «{template_name}» ({language})"
    if variables:
        valores = ", ".join(str(v) for v in variables if str(v).strip())
        if valores:
            return f"{linea} con: {valores}"
    return linea


# ---------------------------------------------------------------------------
# Cierre terminal: ninguna campaña se queda "en curso" sin que nadie lo sepa
# ---------------------------------------------------------------------------


def _fail_job_stmt(job_id: uuid.UUID, reason: str):
    """UPDATE guardado que deja el job en `failed` (sin pisar un cancel/done).

    Cubre también `queued`: si la primera vuelta muere antes de comprometer el
    paso a `running`, el job se quedaba "en cola" para siempre.
    """
    from app.models.outbound_job import (
        JOB_STATUS_FAILED,
        JOB_STATUS_QUEUED,
        JOB_STATUS_RUNNING,
        OutboundJob,
    )

    return (
        update(OutboundJob)
        .where(
            OutboundJob.id == job_id,
            OutboundJob.status.in_((JOB_STATUS_QUEUED, JOB_STATUS_RUNNING)),
        )
        .values(status=JOB_STATUS_FAILED, error=reason[:1000], finished_at=_now())
    )


async def _fail_job(job_id: uuid.UUID, reason: str) -> None:
    async with db_session() as db:
        await db.execute(_fail_job_stmt(job_id, reason))
        await db.commit()


def _fail_job_sync(job_id: str, reason: str) -> None:
    """`_fail_job` desde el cuerpo síncrono de la task, best-effort.

    Si la BD tampoco responde, el log de error de arriba ya dejó constancia; lo
    que no puede pasar es que este intento tape el fallo original.
    """
    try:
        asyncio.run(_fail_job(uuid.UUID(job_id), reason))
        logger.error("outbound_send.job_failed", job_id=job_id, reason=reason[:300])
    except Exception as exc:
        logger.error(
            "outbound_send.job_failed_not_recorded", job_id=job_id, error=str(exc)
        )


def stale_after_seconds(throttle_max_seconds: int) -> float:
    """Cuánto silencio (s) convierte un `running` en "muerto". Función pura.

    Un job vivo escribe en la BD como mucho cada `throttle_max` segundos. Se
    exige que pase MUCHO más que eso (x3) y nunca menos de media hora, para no
    cerrar por error una campaña lenta que sigue perfectamente viva.
    """
    return max(STALE_MIN_SECONDS, int(throttle_max_seconds) * STALE_THROTTLE_FACTOR)


def is_job_stale(
    *, throttle_max_seconds: int, last_activity: datetime | None, now: datetime
) -> bool:
    """¿Un job `running` lleva demasiado sin dar señales? Función pura."""
    if last_activity is None:
        return False
    return (now - last_activity) >= timedelta(
        seconds=stale_after_seconds(throttle_max_seconds)
    )


async def _reap_stale_jobs(now: datetime | None = None) -> int:
    """Cierra en `failed` los jobs en curso que ya no mueve nadie.

    Caso real: el mensaje de re-encolado se pierde (Redis reiniciado, cola
    purgada) y la campaña se queda a medias en "en curso" para siempre. Incluye
    los `queued` cuyo primer mensaje nunca llegó a ejecutarse. Son poquísimas
    filas (jobs sin terminar), así que se revisan una a una.
    """
    from app.models.outbound_job import (
        JOB_STATUS_QUEUED,
        JOB_STATUS_RUNNING,
        OutboundJob,
        OutboundJobRecipient,
    )

    now = now or _now()
    reaped = 0
    async with db_session() as db:
        jobs = (
            await db.execute(
                select(OutboundJob).where(
                    OutboundJob.status.in_((JOB_STATUS_QUEUED, JOB_STATUS_RUNNING))
                )
            )
        ).scalars().all()
        for job in jobs:
            last_sent = (
                await db.execute(
                    select(func.max(OutboundJobRecipient.sent_at)).where(
                        OutboundJobRecipient.job_id == job.id
                    )
                )
            ).scalar_one_or_none()
            marks = [
                d for d in (last_sent, job.started_at, job.created_at) if d is not None
            ]
            last_activity = max(marks) if marks else None
            if not is_job_stale(
                throttle_max_seconds=job.throttle_max_seconds,
                last_activity=last_activity,
                now=now,
            ):
                continue
            quiet_min = int((now - last_activity).total_seconds() // 60)
            reason = (
                f"Envío interrumpido: sin actividad desde hace {quiet_min} min "
                "(el trabajo en segundo plano dejó de ejecutarse). Los "
                "destinatarios pendientes NO se han enviado."
            )
            await db.execute(_fail_job_stmt(job.id, reason))
            await db.commit()
            logger.error(
                "outbound_send.job_stale_failed",
                job_id=str(job.id),
                quiet_minutes=quiet_min,
            )
            reaped += 1
    return reaped


@shared_task(name="app.tasks.outbound_send.reap_stale_jobs", bind=True, max_retries=0)
def reap_stale_jobs(self) -> None:
    """Barrido periódico (beat) de campañas colgadas en "en curso"."""
    try:
        reaped = asyncio.run(_reap_stale_jobs())
    except Exception as exc:
        logger.error("outbound_send.reap_failed", error=str(exc))
        return
    if reaped:
        logger.error("outbound_send.reaped_stale_jobs", count=reaped)
