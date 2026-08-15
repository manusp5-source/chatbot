"""Tool: actualizar el contacto del usuario que está hablando.

SEGURIDAD: el teléfono SIEMPRE es el del webhook (ctx.telefono). Ignoramos
cualquier valor de `args["telefono"]` que el LLM pudiera intentar pasar
inducido por un usuario malicioso ("mi número real es +34999..."). Esto evita
que un atacante en WhatsApp pueda pisar/leakear datos de OTRO contacto.
"""
import json
from typing import Any

from sqlalchemy import select

from app.agents.tools.registry import Tool, register_tool
from app.core.logging import get_logger
from app.db.session import db_session
from app.models.contact import Contact, ContactEstado, ContactOrigen
from app.providers.llm.base import LLMToolSchema
from app.services.security_alerts import notify_security

logger = get_logger(__name__)


async def crear_actualizar_contacto(args: dict[str, Any], ctx: dict[str, Any]) -> str:
    # El teléfono se toma SIEMPRE del contexto del webhook, NUNCA de los args.
    telefono = (ctx.get("telefono") or "").strip()
    if not telefono:
        return json.dumps({"error": "No se ha podido identificar el contacto"})

    # Si el LLM intentó pasar otro teléfono, lo registramos como intento.
    if args.get("telefono") and args.get("telefono").strip() != telefono:
        attempted = args.get("telefono").strip()
        logger.warning(
            "agent.tool.telefono_override_blocked",
            ctx_phone="<PHONE>",
            arg_phone="<PHONE>",
        )
        await notify_security(
            kind="phone_override",
            title="Intento de suplantación de teléfono vía agente",
            details={
                "real_phone": telefono,
                "intento_phone": attempted,
            },
            throttle_key=telefono,
        )

    update_fields: dict[str, Any] = {}
    for k in ("nombre", "email", "servicio_interes"):
        v = args.get(k)
        if v and isinstance(v, str):
            update_fields[k] = v.strip()[:200]  # límite duro para no llenar BD
    if email := update_fields.get("email"):
        update_fields["email"] = email.lower()

    estado_in = args.get("estado")
    if estado_in:
        try:
            update_fields["estado"] = ContactEstado(estado_in)
        except ValueError:
            pass

    # Si el agente captura identidad real (nombre o email), deja de ser un
    # visitante anónimo y pasa a ser un lead de verdad. Voz e Instagram crean
    # el contacto con in_crm=False para no llenar el CRM de gente sin datos;
    # en cuanto dejan nombre/email, se promueve para que aparezca en Contactos.
    captured_identity = bool(update_fields.get("nombre") or update_fields.get("email"))

    async with db_session() as db:
        existing = (
            await db.execute(select(Contact).where(Contact.telefono == telefono))
        ).scalar_one_or_none()
        if existing:
            for k, v in update_fields.items():
                setattr(existing, k, v)
            if captured_identity and not existing.in_crm:
                existing.in_crm = True
            await db.commit()
            await db.refresh(existing)
            return json.dumps(
                {"updated": True, "id": str(existing.id), "nombre": existing.nombre}
            )
        # crear
        contact = Contact(
            telefono=telefono,
            origen=ContactOrigen.whatsapp,
            estado=ContactEstado.contacto,
            **update_fields,
        )
        db.add(contact)
        await db.commit()
        await db.refresh(contact)
        return json.dumps({"created": True, "id": str(contact.id), "nombre": contact.nombre})


register_tool(
    Tool(
        schema=LLMToolSchema(
            name="crear_actualizar_contacto",
            description=(
                "Actualiza los datos del contacto que está hablando (su teléfono "
                "se toma automáticamente de la conversación, NO se acepta como "
                "parámetro). Úsalo para registrar nombre, email, servicio de "
                "interés o estado del lead. No informes al usuario de acciones "
                "internas del CRM."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "nombre": {"type": "string"},
                    "email": {"type": "string"},
                    "servicio_interes": {"type": "string"},
                    "estado": {
                        "type": "string",
                        "enum": [
                            "contacto",
                            "solicitud_presupuesto",
                            "seguimiento",
                            "cliente",
                            "perdido",
                            "no_cualifica",
                        ],
                    },
                },
            },
        ),
        handler=crear_actualizar_contacto,
    )
)
