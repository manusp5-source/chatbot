"""Lo que se descarta en el panel, archivado y etiquetado también en Gmail.

Hasta ahora "Descartados" solo existía dentro del panel: el correo seguía en
Recibidos como si nada. Esto lo saca de Recibidos y le pone una etiqueta, que
es lo que hace un filtro de Gmail de toda la vida. No borra ni marca como spam:
el correo se queda en la etiqueta y se puede recuperar.

Qué toca el buzón lo decide un ajuste (`classifier.gmail_action`):

  rules  (por defecto)  SOLO las reglas duras que escribe el dueño. Lo que
                        decide la IA se queda en el panel. Una regla es
                        literal y no se equivoca; el modelo puede.
  all                   también lo que descarta el clasificador con IA y el
                        filtro de newsletters.
  off                   no se toca Gmail nunca.

Todo es best-effort: si Gmail falla, el mensaje ya está retenido en el panel
(que es lo que de verdad impide que conteste el bot) y solo queda un aviso en
el log.
"""
from __future__ import annotations

import uuid

from sqlalchemy import desc, select

from app.core.logging import get_logger
from app.db.session import db_session
from app.models.message import Message, MessageRole
from app.services.app_settings import get_app_setting

logger = get_logger(__name__)

SETTING_ACTION = "classifier.gmail_action"
SETTING_LABEL = "classifier.gmail_label"

ACTION_RULES = "rules"
ACTION_ALL = "all"
ACTION_OFF = "off"
ACTIONS = (ACTION_RULES, ACTION_ALL, ACTION_OFF)

DEFAULT_ACTION = ACTION_RULES
DEFAULT_LABEL = "Descartado por el bot"

# Etiquetas de sistema de Gmail. Ponerle una de estas a un correo NO es
# archivar: "SPAM" lo marca como spam (y Gmail lo borra a los 30 días) y
# "TRASH" lo tira a la papelera. La API sólo devuelve etiquetas propias desde
# `_find_user_label`, así que esto es un segundo cerrojo por si alguna vez
# llegara un id de sistema por otro camino.
LABELS_SISTEMA = {
    "INBOX", "SPAM", "TRASH", "SENT", "DRAFT", "UNREAD", "STARRED", "IMPORTANT",
    "CHAT", "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES", "CATEGORY_FORUMS",
}

# Tope de mensajes al devolver a Recibidos. Un hilo descartado normal tiene uno
# o dos; el tope evita que uno largo dispare cincuenta llamadas a Gmail.
_MAX_MENSAJES = 20

# Marca en `Message.extra`: lo puso ahí ESTE código al archivar, y guarda el id
# de la etiqueta que usó. Sin ella, devolver a Recibidos operaba sobre todo el
# hilo y resucitaba correos que la dueña había archivado a mano; y si alguien
# cambiaba el nombre de la etiqueta, el restore quitaba otra distinta.
MARCA_ARCHIVADO = "gmail_archived_label_id"


async def get_gmail_action() -> str:
    action = (await get_app_setting(SETTING_ACTION, DEFAULT_ACTION)).strip().lower()
    return action if action in ACTIONS else DEFAULT_ACTION


async def get_label_name() -> str:
    return (await get_app_setting(SETTING_LABEL, DEFAULT_LABEL)).strip() or DEFAULT_LABEL


async def _mensajes_a_archivar(message_ids: list[uuid.UUID]) -> list[tuple[uuid.UUID, str]]:
    """(id interno, id de Gmail) de los mensajes ENTRANTES indicados.

    Solo se archiva lo que disparó la retención, no el hilo entero: en email la
    conversación ES el hilo de Gmail, y sacar de Recibidos ocho correos
    anteriores que nadie había descartado por culpa del noveno es justo lo que
    no debe pasar. Solo mensajes del cliente (`rol=user`): lo que enviamos
    nosotros desde el panel no se toca.
    """
    if not message_ids:
        return []
    async with db_session() as db:
        rows = (
            await db.execute(
                select(Message.id, Message.extra["provider_message_id"].astext).where(
                    Message.id.in_(message_ids),
                    Message.rol == MessageRole.user,
                )
            )
        ).all()
    return [(mid, pmid) for mid, pmid in rows if pmid]


async def _mensajes_archivados(conversation_id: uuid.UUID) -> list[tuple[uuid.UUID, str, str]]:
    """(id interno, id de Gmail, id de etiqueta) de lo que archivamos NOSOTROS."""
    async with db_session() as db:
        rows = (
            await db.execute(
                select(
                    Message.id,
                    Message.extra["provider_message_id"].astext,
                    Message.extra[MARCA_ARCHIVADO].astext,
                )
                .where(
                    Message.conversation_id == conversation_id,
                    Message.rol == MessageRole.user,
                    Message.extra[MARCA_ARCHIVADO].astext.isnot(None),
                )
                .order_by(desc(Message.created_at))
                .limit(_MAX_MENSAJES)
            )
        ).all()
    return [(mid, pmid, lab) for mid, pmid, lab in rows if pmid and lab]


async def _marcar(message_id: uuid.UUID, label_id: str | None) -> None:
    """Apunta (o borra) en el mensaje la etiqueta con la que lo archivamos."""
    async with db_session() as db:
        msg = (
            await db.execute(select(Message).where(Message.id == message_id))
        ).scalar_one_or_none()
        if msg is None:
            return
        extra = dict(msg.extra or {})
        if label_id:
            extra[MARCA_ARCHIVADO] = label_id
        else:
            extra.pop(MARCA_ARCHIVADO, None)
        msg.extra = extra  # reasignar: JSONB no detecta mutaciones in-place
        await db.commit()


async def _should_touch_gmail(canal: str, by_rule: bool) -> bool:
    if canal != "email":
        return False
    action = await get_gmail_action()
    if action == ACTION_OFF:
        return False
    if action == ACTION_RULES and not by_rule:
        return False
    return True


async def _archivar_mensajes(
    gmail,
    mensajes: list[tuple[uuid.UUID, str]],
    label_id: str,
    nombre_etiqueta: str,
) -> int:
    """Saca de Recibidos y etiqueta. En UNA llamada si se puede.

    Un hilo descartado suele traer varios correos, y antes cada uno salía en su
    propia petición HTTP a Gmail, en serie. `batchModify` los hace todos de
    golpe, pero es todo o nada: responde 204 sin cuerpo y no dice cuál falló.
    De ahí las dos vueltas —si el lote no pasa, se va de uno en uno para que un
    identificador malo no se lleve por delante el archivado del resto.

    Un 4xx del lote suele significar que el id de etiqueta ya no vale porque
    alguien la borró en Gmail, así que se olvida el que había cacheado: la
    próxima vez se vuelve a preguntar y, si hace falta, se crea otra.
    """
    ids_gmail = [gmail_id for _, gmail_id in mensajes]
    if len(ids_gmail) > 1 and await gmail.batch_modify_messages(
        ids_gmail, add=[label_id], remove=["INBOX"]
    ):
        for msg_id, _ in mensajes:
            await _marcar(msg_id, label_id)
        return len(mensajes)

    tocados = 0
    for msg_id, gmail_id in mensajes:
        if await gmail.modify_message(gmail_id, add=[label_id], remove=["INBOX"]):
            await _marcar(msg_id, label_id)
            tocados += 1
    if tocados == 0 and mensajes:
        from app.providers.gmail.client import olvidar_label_id

        await olvidar_label_id(nombre_etiqueta)
        logger.info("gmail_quarantine.label_cache_olvidada", etiqueta=nombre_etiqueta)
    return tocados


async def archive_in_gmail(
    conversation_id: uuid.UUID,
    canal: str,
    by_rule: bool,
    message_ids: list[uuid.UUID] | None = None,
) -> bool:
    """Saca de Recibidos y etiqueta los correos que provocaron el descarte.

    `message_ids` son los mensajes de ESTA tanda. Devuelve True si se tocó al
    menos uno. No lanza nunca: archivar es un extra, y el mensaje ya está
    retenido en el panel, que es lo que impide que conteste el bot.
    """
    try:
        if not await _should_touch_gmail(canal, by_rule):
            return False
        mensajes = await _mensajes_a_archivar(message_ids or [])
        if not mensajes:
            return False
        from app.providers.gmail import get_gmail_provider

        gmail = get_gmail_provider()
        nombre_etiqueta = await get_label_name()
        label_id = await gmail.ensure_label(nombre_etiqueta)
        # Un id de etiqueta de sistema aquí significaría marcar como spam o
        # tirar a la papelera: mejor no archivar que hacer algo irreversible.
        if not label_id or label_id in LABELS_SISTEMA:
            if label_id:
                logger.warning("gmail_quarantine.label_de_sistema", label_id=label_id)
            return False
        tocados = await _archivar_mensajes(gmail, mensajes, label_id, nombre_etiqueta)
        logger.info(
            "gmail_quarantine.archived",
            conversation_id=str(conversation_id),
            mensajes=tocados,
            por_regla=by_rule,
        )
        return tocados > 0
    except Exception as e:  # nunca puede tumbar el flujo de entrada
        logger.warning(
            "gmail_quarantine.archive_failed",
            conversation_id=str(conversation_id),
            error=str(e),
        )
        return False


async def archive_followup_in_gmail(
    conversation_id: uuid.UUID, canal: str, message_id: uuid.UUID
) -> bool:
    """Archiva un correo que llega a un hilo YA descartado y ya archivado.

    No decide nada: solo continúa lo que se decidió cuando se retuvo el hilo.
    Por eso mira si hay algún mensaje con la marca en vez de volver a evaluar
    `classifier.gmail_action` — si el hilo lo retuvo la IA y el ajuste dice que
    la IA no toca el buzón, aquí no hay ninguna marca y no se archiva nada.
    """
    try:
        if canal != "email":
            return False
        if not await _mensajes_archivados(conversation_id):
            return False
        mensajes = await _mensajes_a_archivar([message_id])
        if not mensajes:
            return False
        from app.providers.gmail import get_gmail_provider

        gmail = get_gmail_provider()
        label_id = await gmail.ensure_label(await get_label_name())
        if not label_id or label_id in LABELS_SISTEMA:
            return False
        msg_id, gmail_id = mensajes[0]
        if not await gmail.modify_message(gmail_id, add=[label_id], remove=["INBOX"]):
            return False
        await _marcar(msg_id, label_id)
        logger.info(
            "gmail_quarantine.archived_followup", conversation_id=str(conversation_id)
        )
        return True
    except Exception as e:
        logger.warning(
            "gmail_quarantine.followup_failed",
            conversation_id=str(conversation_id),
            error=str(e),
        )
        return False


async def restore_in_gmail(conversation_id: uuid.UUID, canal: str) -> bool:
    """Deshace lo anterior al liberar la conversación: vuelve a Recibidos y
    se le quita la etiqueta.

    Toca EXACTAMENTE los mensajes que archivamos nosotros —los que llevan la
    marca— y les quita la etiqueta con la que se archivaron, no la que esté
    configurada hoy. Antes devolvía a Recibidos todo el hilo: eso resucitaba
    correos viejos que la dueña había archivado a mano, que es lo contrario de
    deshacer.

    No mira el ajuste `classifier.gmail_action`: si se apagó DESPUÉS de haber
    archivado, el correo seguiría fuera de Recibidos y liberarlo desde el panel
    no lo devolvería.
    """
    try:
        if canal != "email":
            return False
        mensajes = await _mensajes_archivados(conversation_id)
        if not mensajes:
            return False
        from app.providers.gmail import get_gmail_provider

        gmail = get_gmail_provider()
        tocados = 0
        for msg_id, gmail_id, label_id in mensajes:
            if await gmail.modify_message(gmail_id, add=["INBOX"], remove=[label_id]):
                await _marcar(msg_id, None)
                tocados += 1
        logger.info(
            "gmail_quarantine.restored",
            conversation_id=str(conversation_id),
            mensajes=tocados,
            pendientes=len(mensajes) - tocados,
        )
        return tocados > 0
    except Exception as e:
        logger.warning(
            "gmail_quarantine.restore_failed",
            conversation_id=str(conversation_id),
            error=str(e),
        )
        return False
