"""Tool: consultar los datos del contacto del usuario que está hablando.

SEGURIDAD: solo devuelve el contacto cuyo teléfono coincide con el del
webhook (ctx.telefono). Antes la tool aceptaba `telefono` o `email` arbitrarios
en args, lo que permitía a un usuario WhatsApp pedirle al agente que buscara
"el contacto con email jefe@empresa.com" y filtrar PII de cualquier contacto.
"""
import json
from typing import Any

from sqlalchemy import select

from app.agents.tools.registry import Tool, register_tool
from app.core.logging import get_logger
from app.db.session import db_session
from app.models.contact import Contact
from app.providers.llm.base import LLMToolSchema

logger = get_logger(__name__)


async def buscar_contacto(_args: dict[str, Any], ctx: dict[str, Any]) -> str:
    telefono = (ctx.get("telefono") or "").strip()
    if not telefono:
        return json.dumps({"found": False, "error": "Contacto no identificado"})

    async with db_session() as db:
        contact = (
            await db.execute(select(Contact).where(Contact.telefono == telefono))
        ).scalar_one_or_none()
        if not contact:
            return json.dumps({"found": False})
        return json.dumps(
            {
                "found": True,
                "id": str(contact.id),
                "nombre": contact.nombre,
                # Devolvemos los campos que el agente necesita para conversar.
                # NUNCA devolvemos teléfono/email de OTROS contactos.
                "email": contact.email,
                "estado": contact.estado.value,
                "servicio_interes": contact.servicio_interes,
            }
        )


register_tool(
    Tool(
        schema=LLMToolSchema(
            name="buscar_contacto",
            description=(
                "Recupera los datos del contacto que está hablando (su teléfono "
                "se toma automáticamente de la conversación, no se acepta como "
                "parámetro). Útil para personalizar la respuesta o ver el estado "
                "del lead. NO puede usarse para consultar otros contactos del CRM."
            ),
            parameters={"type": "object", "properties": {}},
        ),
        handler=buscar_contacto,
    )
)
