import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.db.session import get_db
from app.models.contact import ContactTag
from app.models.tag import Tag
from app.models.user import User
from app.schemas.common import OkResponse
from app.services.audit import record_audit

router = APIRouter(prefix="/tags", tags=["tags"])

# Aceptamos solo color hex #RRGGBB (los valores se interpolan en style del FE).
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _validate_hex_color(value: str) -> str:
    if not _HEX_COLOR_RE.match(value):
        raise ValueError("El color debe ser hex en formato #RRGGBB")
    return value.lower()


class TagOut(BaseModel):
    id: uuid.UUID
    nombre: str
    color: str

    class Config:
        from_attributes = True


class TagCreate(BaseModel):
    nombre: str = Field(min_length=1, max_length=60)
    color: str = "#3b82f6"

    @field_validator("color")
    @classmethod
    def _check_color(cls, v: str) -> str:
        return _validate_hex_color(v)


class TagUpdate(BaseModel):
    nombre: str | None = Field(default=None, min_length=1, max_length=60)
    color: str | None = None

    @field_validator("color")
    @classmethod
    def _check_color(cls, v: str | None) -> str | None:
        return _validate_hex_color(v) if v is not None else None


@router.get("")
async def list_tags(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)) -> list[TagOut]:
    return [TagOut.model_validate(t) for t in (await db.execute(select(Tag).order_by(Tag.nombre))).scalars().all()]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_tag(
    payload: TagCreate, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)
) -> TagOut:
    existing = (await db.execute(select(Tag).where(Tag.nombre == payload.nombre))).scalar_one_or_none()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ya existe una etiqueta con ese nombre")
    tag = Tag(nombre=payload.nombre, color=payload.color)
    db.add(tag)
    await db.commit()
    await db.refresh(tag)
    return TagOut.model_validate(tag)


@router.patch("/{tag_id}")
async def update_tag(
    tag_id: uuid.UUID,
    payload: TagUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> TagOut:
    tag = (await db.execute(select(Tag).where(Tag.id == tag_id))).scalar_one_or_none()
    if not tag:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    data = payload.model_dump(exclude_unset=True)
    # Renombrar a un nombre que YA existe reventaba con un 500 (choque contra la
    # restricción de unicidad al hacer commit) y en pantalla se veía como "algo
    # ha fallado". Es un choque previsible y tiene su código: 409, con el mismo
    # mensaje que al crear una etiqueta repetida.
    nuevo_nombre = data.get("nombre")
    if nuevo_nombre is not None and nuevo_nombre != tag.nombre:
        choque = (
            await db.execute(
                select(Tag.id).where(Tag.nombre == nuevo_nombre, Tag.id != tag_id)
            )
        ).scalar_one_or_none()
        if choque:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Ya existe una etiqueta con ese nombre"
            )
    for k, v in data.items():
        setattr(tag, k, v)
    await db.commit()
    await db.refresh(tag)
    return TagOut.model_validate(tag)


@router.delete("/{tag_id}", response_model=OkResponse)
async def delete_tag(
    tag_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> OkResponse:
    """Borra una etiqueta. SOLO admin: no es "su" etiqueta, es la de todos.

    Al borrarla desaparece de TODAS las conversaciones y contactos que la
    tuvieran (cascade en contact_tags) y no hay vuelta atrás. Antes lo podía
    hacer cualquier sesión y no quedaba rastro de quién.
    """
    tag = (await db.execute(select(Tag).where(Tag.id == tag_id))).scalar_one_or_none()
    if not tag:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    # Cuántos contactos pierden la etiqueta (dato del registro: sin PII).
    afectados = (
        await db.execute(
            select(func.count()).select_from(ContactTag).where(ContactTag.tag_id == tag_id)
        )
    ).scalar_one()
    nombre, color = tag.nombre, tag.color
    await db.delete(tag)
    await record_audit(
        db,
        user_id=current_user.id,
        action="tag.deleted",
        entity="tag",
        entity_id=tag_id,
        before={"nombre": nombre, "color": color},
        after={"contactos_afectados": int(afectados or 0)},
    )
    await db.commit()
    return OkResponse()
