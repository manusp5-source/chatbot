"""Fusionar dos fichas del mismo cliente en una.

Estaba escrito a pelo dentro del endpoint `POST /contacts/{id}/merge`. Ahora
vive aquí porque hay un SEGUNDO sitio que lo necesita: la entrada de mensajes,
cuando descubre sola que dos fichas son la misma persona (ver
`_resolver_contacto_whatsapp` en `services/conversation.py`). Dos copias de una
operación irreversible sobre datos de clientes acabarían divergiendo.

Qué hace, en la transacción de quien llama:
  - mueve conversaciones, notas y actividad del origen al destino (los mensajes
    viajan con su conversación);
  - copia las etiquetas que le faltaran al destino;
  - rellena los HUECOS del destino con lo que tuviera el origen. Lo que el
    destino ya tenía NO se pisa: fusionar no puede empeorar la ficha buena;
  - se queda con la fecha de alta más antigua y la actividad más reciente;
  - hereda el BSUID de WhatsApp si el destino no tenía. Esto es lo que evita
    que la fusión ROMPA la identidad: sin ello, el siguiente mensaje que
    llegara solo con el BSUID no encontraría la ficha fusionada y volvería a
    crear la duplicada que se acababa de deshacer;
  - borra la ficha de origen.

NO hace la auditoría ni el commit: eso es de quien llama, que es el único que
sabe si detrás hay una persona (endpoint, con su usuario) o la propia ingesta.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.contact import Contact, ContactTag
from app.models.contact_activity import ContactActivity
from app.models.contact_note import ContactNote
from app.models.conversation import Conversation

logger = get_logger(__name__)

# Campos de la ficha que se copian del origen SOLO si el destino los tiene
# vacíos. `wa_user_id` no está aquí: tiene un índice único y hay que moverlo
# después de borrar el origen (ver abajo).
CAMPOS_RELLENABLES = (
    "nombre",
    "email",
    "servicio_interes",
    "empresa",
    "cargo",
    "web",
    "nif",
    "direccion",
    "social_handle",
)


async def fusionar_contactos(
    db: AsyncSession, destino: Contact, origen: Contact
) -> dict:
    """Vuelca `origen` sobre `destino` y borra `origen`. Devuelve el resumen.

    Sin commit: la transacción la cierra quien llama.
    """
    if destino.id == origen.id:
        raise ValueError("No se puede fusionar un contacto consigo mismo")

    # 0) Cerrojo de fila sobre las DOS fichas, y siempre en el mismo orden (por
    #    id) para que dos fusiones simultáneas no se abracen.
    #
    #    Sin esto, otra ingesta en paralelo podía estar insertando una
    #    conversación nueva apuntando al origen justo mientras aquí se borra:
    #    el `UPDATE` de más abajo no la veía (aún sin confirmar) y el borrado se
    #    la llevaba por delante en cascada, con sus mensajes dentro. Un mensaje
    #    de cliente perdido y sin traza.
    #
    #    Con el cerrojo, esa otra ingesta se queda esperando —insertar una fila
    #    con clave ajena pide un bloqueo sobre la fila padre— y al soltarlo se
    #    encuentra con que el origen ya no existe: su transacción falla, el
    #    proveedor reintenta el webhook y esta vez la ficha que encuentra es la
    #    fusionada.
    for contact_id in sorted([destino.id, origen.id], key=str):
        await db.execute(select(Contact.id).where(Contact.id == contact_id).with_for_update())

    # 1) Rellenar huecos del destino (nunca pisar lo que ya tenía).
    campos_rellenados: list[str] = []
    for campo in CAMPOS_RELLENABLES:
        if not getattr(destino, campo, None) and getattr(origen, campo, None):
            setattr(destino, campo, getattr(origen, campo))
            campos_rellenados.append(campo)
    # Notas internas: si las dos tienen, se concatenan (no se pierde ninguna).
    if origen.notas_internas:
        if destino.notas_internas and origen.notas_internas not in destino.notas_internas:
            destino.notas_internas = f"{destino.notas_internas}\n---\n{origen.notas_internas}"
        elif not destino.notas_internas:
            destino.notas_internas = origen.notas_internas
    # Fechas: alta más antigua, última actividad más reciente.
    if origen.created_at and destino.created_at and origen.created_at < destino.created_at:
        destino.created_at = origen.created_at
    if origen.ultimo_mensaje_at and (
        not destino.ultimo_mensaje_at or origen.ultimo_mensaje_at > destino.ultimo_mensaje_at
    ):
        destino.ultimo_mensaje_at = origen.ultimo_mensaje_at
    # Si el origen estaba en el CRM, el fusionado también.
    if origen.in_crm:
        destino.in_crm = True

    # 2) Etiquetas que le falten al destino (la tabla tiene PK compuesta:
    #    reasignar a ciegas chocaría con las que ya comparten).
    tags_destino = {
        t
        for t in (
            await db.execute(
                select(ContactTag.tag_id).where(ContactTag.contact_id == destino.id)
            )
        )
        .scalars()
        .all()
    }
    tags_origen = (
        await db.execute(select(ContactTag.tag_id).where(ContactTag.contact_id == origen.id))
    ).scalars().all()
    for tag_id in tags_origen:
        if tag_id not in tags_destino:
            db.add(ContactTag(contact_id=destino.id, tag_id=tag_id))

    # 3) Mover conversaciones, notas y actividad (UPDATE en bloque, no fila a
    #    fila: una ficha con años de histórico puede tener miles de filas).
    n_convs = (
        await db.execute(
            sa_update(Conversation)
            .where(Conversation.contact_id == origen.id)
            .values(contact_id=destino.id)
        )
    ).rowcount or 0
    n_notas = (
        await db.execute(
            sa_update(ContactNote)
            .where(ContactNote.contact_id == origen.id)
            .values(contact_id=destino.id)
        )
    ).rowcount or 0
    n_actividad = (
        await db.execute(
            sa_update(ContactActivity)
            .where(ContactActivity.contact_id == origen.id)
            .values(contact_id=destino.id)
        )
    ).rowcount or 0

    # 4) Fuera la ficha de origen (sus contact_tags caen por cascade).
    bsuid_heredado = origen.wa_user_id
    parent_heredado = origen.wa_parent_user_id
    telefono_origen = origen.telefono
    await db.delete(origen)

    # 5) El BSUID, DESPUÉS de borrar el origen y con el borrado ya en la base.
    #    El índice único de `wa_user_id` se comprueba al terminar cada
    #    sentencia, no al confirmar: asignárselo al destino con el origen
    #    todavía vivo reventaría por duplicado.
    #
    #    Sin este paso la fusión rompía la identidad que arreglaba: el mensaje
    #    siguiente llegaba solo con el BSUID, no encontraba a nadie, y creaba
    #    otra vez la ficha duplicada que se acababa de deshacer.
    await db.flush()
    heredado = False
    if bsuid_heredado and not destino.wa_user_id:
        destino.wa_user_id = bsuid_heredado
        destino.wa_parent_user_id = destino.wa_parent_user_id or parent_heredado
        heredado = True
        await db.flush()
    elif bsuid_heredado and destino.wa_user_id != bsuid_heredado:
        # Dos BSUID distintos para la misma persona. Pasa si el cliente
        # regeneró el suyo. Nos quedamos con el del destino —el que usan los
        # mensajes que van a seguir llegando— y dejamos constancia de que el
        # otro deja de existir, porque nada más lo va a recordar.
        logger.warning(
            "contacts.merge.bsuid_descartado",
            destino=str(destino.id),
            descartado=bsuid_heredado[:24],
        )

    return {
        "conversaciones_movidas": int(n_convs),
        "notas_movidas": int(n_notas),
        "eventos_movidos": int(n_actividad),
        "campos_rellenados": campos_rellenados,
        "bsuid_heredado": heredado,
        "bsuid_descartado": bool(
            bsuid_heredado and not heredado and destino.wa_user_id != bsuid_heredado
        ),
        "telefono_origen_era_identificador": _es_identificador_de_canal(telefono_origen),
    }


def _es_identificador_de_canal(valor: str) -> bool:
    """`wa:`, `ig:`, `web:`… en vez de un teléfono de verdad.

    Import local: la lista canónica vive en la capa de API y un servicio no
    puede importarla arriba sin montar un ciclo.
    """
    from app.api.contacts import is_channel_identifier

    return is_channel_identifier(valor)
