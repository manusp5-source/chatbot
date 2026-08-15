"""Servir adjuntos del operador con autenticación.

Estos archivos pueden contener info sensible del cliente (PDF con DNI,
imagen privada). NO los exponemos públicamente — solo a usuarios del panel
autenticados con JWT.

WhatsApp NO descarga de aquí: para enviar al cliente usamos el media_id
que YCloud guarda internamente. Este endpoint existe solo para que el
inbox del panel muestre el adjunto en el histórico.

Además de la sesión válida se comprueba que el fichero PERTENEZCA a una
conversación (ver `attachment_belongs_to_conversation`). Antes bastaba con
saber el nombre: cualquier usuario identificado descargaba cualquier fichero
del volumen, incluidos los que ya no cuelgan de ninguna conversación (restos de
subidas abortadas, ficheros de contactos borrados a medias). Los nombres son
identificadores aleatorios y no se adivinan, pero "no se adivina" no es un
control de acceso.
"""
from __future__ import annotations

import mimetypes
import os

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.ratelimit import limit_spec, make_limiter
from app.db.session import get_db
from app.models.user import User
from app.services.media import resolve_local_path

router = APIRouter(prefix="/uploads", tags=["uploads"])
limiter = make_limiter()


async def attachment_belongs_to_conversation(
    db: AsyncSession, prefix: str, filename: str
) -> bool:
    """¿Ese fichero está referenciado por algún mensaje de alguna conversación?

    `prefix` es "/uploads/" o "/audios/" (la URL tal cual se guarda en
    `Message.media_url` / `Message.audio_url`).

    Hoy todos los roles del panel ven TODAS las conversaciones (no hay bandejas
    asignadas por operador), así que "una conversación que pueda ver" equivale a
    "una conversación". Si algún día se reparte la bandeja, este es el único
    sitio que hay que tocar: filtrar además por las conversaciones del usuario.
    """
    if not filename:
        return False
    url = f"{prefix}{filename}"
    from app.models.message import Message

    found = (
        await db.execute(
            select(Message.id)
            .where(or_(Message.media_url == url, Message.audio_url == url))
            .limit(1)
        )
    ).scalar_one_or_none()
    return found is not None


# `response_class` en vez de anotar el retorno: con `from __future__ import
# annotations` el decorador del rate-limit envuelve la función y FastAPI ya no
# puede resolver el nombre `FileResponse` de la anotación (queda como texto).
@router.get("/{filename}", response_class=FileResponse)
@limiter.limit(limit_spec("240/minute"))
async def get_upload(
    request: Request,
    filename: str,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    abs_path = resolve_local_path(filename)
    if not abs_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Archivo no encontrado")
    # Mismo 404 que "no existe": no confirmamos la existencia de un fichero
    # que el usuario no debería poder pedir.
    if not await attachment_belongs_to_conversation(db, "/uploads/", filename):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Archivo no encontrado")
    mime, _enc = mimetypes.guess_type(abs_path)
    return FileResponse(
        abs_path,
        media_type=mime or "application/octet-stream",
        filename=os.path.basename(abs_path),
    )
