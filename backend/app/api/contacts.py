import csv
import io
import re
import uuid
from datetime import datetime, timedelta, timezone
# `Annotated[int, Query(...)]` en vez de `= Query(200, ...)`: así el valor por
# defecto del parámetro es un int de verdad. Varios tests llaman a estos
# endpoints como funciones normales (sin pasar por FastAPI) y con la forma
# antigua les llegaba el objeto Query como si fuera el número.
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.core.ratelimit import limit_spec, make_limiter
from app.db.session import get_db
from app.models.contact import Contact, ContactEstado, ContactOrigen, ContactTag
from app.models.contact_activity import ContactActivity
from app.models.contact_note import ContactNote
from app.models.tag import Tag
from app.models.user import User, UserRole
from app.schemas.common import OkResponse, Page
from app.services.contact_activity import record_activity

router = APIRouter(prefix="/contacts", tags=["contacts"])
limiter = make_limiter()

# Identificadores que NO son teléfonos: cada canal guarda su propia llave en la
# columna `telefono` (es la única llave del contacto). Tocarlos a mano rompe el
# emparejamiento de mensajes entrantes con su ficha, así que ni se normalizan ni
# se dejan editar.
CHANNEL_ID_PREFIXES = ("wa:", "ig:", "web:", "email:", "voice:")


def is_channel_identifier(value: str) -> bool:
    """True si `value` es un identificador de canal y no un teléfono."""
    return (value or "").strip().lower().startswith(CHANNEL_ID_PREFIXES)


def normalize_phone(value: str) -> str:
    """Normaliza un teléfono a E.164 (+34600111222).

    Reutiliza la función del proveedor de WhatsApp (`_normalize_phone` de
    ycloud) — que es la que ya decide cómo se guarda un número cuando entra un
    mensaje — en vez de escribir una segunda regla que acabaría divergiendo. Lo
    único que añade aquí es limpiar los separadores humanos (espacios, guiones,
    puntos y paréntesis) que la gente escribe en un formulario o en un Excel:
    "600 111 222" y "+34-600-111-222" tienen que acabar en la MISMA ficha.

    Los identificadores de canal (`wa:`, `ig:`, `web:`…) se devuelven intactos.
    """
    from app.providers.whatsapp.ycloud import _normalize_phone

    s = (value or "").strip()
    if not s or is_channel_identifier(s):
        return s
    # Separadores que no aportan nada al número.
    for ch in (" ", "-", ".", "(", ")", " ", "/"):
        s = s.replace(ch, "")
    if not s:
        return ""
    # "0034600111222" y "0034 600..." → prefijo internacional en formato 00.
    if s.startswith("00"):
        s = "+" + s[2:]
    return _normalize_phone(s)


class TagOut(BaseModel):
    id: uuid.UUID
    nombre: str
    color: str

    class Config:
        from_attributes = True


class ContactOut(BaseModel):
    id: uuid.UUID
    telefono: str
    email: str | None
    nombre: str | None
    estado: str
    origen: str
    servicio_interes: str | None
    empresa: str | None = None
    cargo: str | None = None
    web: str | None = None
    nif: str | None = None
    direccion: str | None = None
    ultimo_mensaje_at: str | None
    notas_internas: str | None
    in_crm: bool = True
    created_at: str | None = None
    updated_at: str | None = None
    tags: list[TagOut] = []

    @classmethod
    def from_orm(cls, contact: Contact, tags: list[Tag] | None = None) -> "ContactOut":
        return cls(
            id=contact.id,
            telefono=contact.telefono,
            email=contact.email,
            nombre=contact.nombre,
            estado=contact.estado.value,
            origen=contact.origen.value,
            servicio_interes=contact.servicio_interes,
            empresa=contact.empresa,
            cargo=contact.cargo,
            web=contact.web,
            nif=contact.nif,
            direccion=contact.direccion,
            ultimo_mensaje_at=contact.ultimo_mensaje_at.isoformat() if contact.ultimo_mensaje_at else None,
            notas_internas=contact.notas_internas,
            in_crm=contact.in_crm,
            created_at=contact.created_at.isoformat() if contact.created_at else None,
            updated_at=contact.updated_at.isoformat() if contact.updated_at else None,
            tags=[TagOut.model_validate(t) for t in (tags or [])],
        )


class ContactCreate(BaseModel):
    # Teléfono OPCIONAL: hay clientes que solo existen por correo (canal Email)
    # y no hay motivo para inventarles un número. Si no viene teléfono se exige
    # email y la ficha se abre con la llave `email:<direccion>`, que es
    # exactamente la que usa el canal de correo, así que cuando ese cliente
    # escriba, su mensaje cae en ESTA ficha y no en una nueva.
    telefono: str | None = Field(None, min_length=3, max_length=320)
    email: EmailStr | None = None
    nombre: str | None = None
    estado: ContactEstado = ContactEstado.contacto
    servicio_interes: str | None = None
    # Ficha CRM (todos opcionales).
    empresa: str | None = Field(None, max_length=200)
    cargo: str | None = Field(None, max_length=120)
    web: str | None = Field(None, max_length=255)
    nif: str | None = Field(None, max_length=32)
    direccion: str | None = Field(None, max_length=500)
    notas_internas: str | None = None


class ContactUpdate(BaseModel):
    # Teléfono EDITABLE. Antes no estaba en el esquema: si te equivocabas al
    # crear la ficha a mano, la única salida era borrarla y rehacerla, y con
    # ella se iban conversaciones, notas y actividad. Ojo: el teléfono es la
    # LLAVE del contacto (por ahí se emparejan los mensajes entrantes), así que
    # el endpoint lo normaliza a E.164, comprueba que no choque con otra ficha,
    # se niega a tocar identificadores de canal (wa:, ig:, web:, email:, voice:)
    # y deja rastro en la actividad.
    telefono: str | None = Field(None, min_length=3, max_length=320)
    email: EmailStr | None = None
    nombre: str | None = None
    estado: ContactEstado | None = None
    servicio_interes: str | None = None
    # Ficha CRM (todos opcionales).
    empresa: str | None = Field(None, max_length=200)
    cargo: str | None = Field(None, max_length=120)
    web: str | None = Field(None, max_length=255)
    nif: str | None = Field(None, max_length=32)
    direccion: str | None = Field(None, max_length=500)
    notas_internas: str | None = None


class NoteAuthorOut(BaseModel):
    id: uuid.UUID
    nombre: str | None
    role: str


class ContactNoteOut(BaseModel):
    id: uuid.UUID
    texto: str
    created_at: str
    author: NoteAuthorOut | None = None

    @classmethod
    def from_orm(cls, note: ContactNote, author: User | None) -> "ContactNoteOut":
        return cls(
            id=note.id,
            texto=note.texto,
            created_at=note.created_at.isoformat() if note.created_at else "",
            author=(
                NoteAuthorOut(id=author.id, nombre=author.nombre, role=author.role.value)
                if author
                else None
            ),
        )


class ContactNoteCreate(BaseModel):
    # Texto de la nota. No vacío y con límite razonable para evitar abuso.
    texto: str = Field(min_length=1, max_length=5000)


class ActivityActorOut(BaseModel):
    id: uuid.UUID
    nombre: str | None
    role: str


class ContactActivityOut(BaseModel):
    """Un evento del timeline de actividad de la ficha de contacto."""

    id: uuid.UUID
    tipo: str
    # meta es un payload pequeño SIN PII (estados / etiqueta / canal / ids).
    meta: dict | None = None
    created_at: str
    actor: ActivityActorOut | None = None

    @classmethod
    def from_orm(cls, ev: ContactActivity, actor: User | None) -> "ContactActivityOut":
        return cls(
            id=ev.id,
            tipo=ev.tipo,
            meta=ev.meta,
            created_at=ev.created_at.isoformat() if ev.created_at else "",
            actor=(
                ActivityActorOut(id=actor.id, nombre=actor.nombre, role=actor.role.value)
                if actor
                else None
            ),
        )


async def _load_tags(db: AsyncSession, contact_id: uuid.UUID) -> list[Tag]:
    return list(
        (
            await db.execute(
                select(Tag).join(ContactTag, Tag.id == ContactTag.tag_id).where(ContactTag.contact_id == contact_id)
            )
        ).scalars().all()
    )


async def _load_tags_bulk(
    db: AsyncSession, contact_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[Tag]]:
    """Etiquetas de VARIOS contactos en UNA sola query.

    El listado hacía un SELECT de etiquetas por cada fila: con page_size=100,
    101 consultas para pintar una página. Aquí se traen todas de una y se
    agrupan en memoria."""
    if not contact_ids:
        return {}
    rows = (
        await db.execute(
            select(ContactTag.contact_id, Tag)
            .join(Tag, Tag.id == ContactTag.tag_id)
            .where(ContactTag.contact_id.in_(contact_ids))
        )
    ).all()
    out: dict[uuid.UUID, list[Tag]] = {}
    for cid, tag in rows:
        out.setdefault(cid, []).append(tag)
    return out


@router.get("")
async def list_contacts(
    search: str | None = None,
    tag_id: uuid.UUID | None = None,
    estado: ContactEstado | None = None,
    origen: ContactOrigen | None = None,
    segment: str | None = Query(None, description="nuevos | activos (ventana 30 días)"),
    sort: str = Query("recent", description="recent | name | created"),
    include_outside_crm: bool = Query(
        False,
        description=(
            "Si True, incluye también contactos que aún no están en el CRM "
            "(visitors de Instagram que no han sido promovidos)."
        ),
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Page[ContactOut]:
    q = select(Contact)
    # Por defecto, los contactos "outside CRM" (in_crm=False) no aparecen
    # en la lista de Contactos. El administrador los ve solo en el inbox y decide
    # manualmente si los promueve al CRM con "Añadir al CRM".
    if not include_outside_crm:
        q = q.where(Contact.in_crm.is_(True))
    if search:
        # Escapamos los comodines LIKE (% _ \) para buscar el término LITERAL
        # (ni comodín involuntario ni escaneo caro).
        def _esc_like(s: str) -> str:
            return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

        like = f"%{_esc_like(search.lower())}%"
        like_raw = f"%{_esc_like(search)}%"
        q = q.where(
            or_(
                func.lower(Contact.nombre).like(like, escape="\\"),
                func.lower(Contact.email).like(like, escape="\\"),
                Contact.telefono.like(like_raw, escape="\\"),
            )
        )
    if estado:
        q = q.where(Contact.estado == estado)
    if origen:
        q = q.where(Contact.origen == origen)
    if tag_id:
        q = q.join(ContactTag, Contact.id == ContactTag.contact_id).where(ContactTag.tag_id == tag_id)
    if segment == "nuevos":
        q = q.where(Contact.created_at >= datetime.now(timezone.utc) - timedelta(days=30))
    elif segment == "activos":
        q = q.where(Contact.ultimo_mensaje_at >= datetime.now(timezone.utc) - timedelta(days=30))

    if sort == "name":
        order_by = [func.lower(Contact.nombre).asc().nullslast()]
    elif sort == "created":
        order_by = [desc(Contact.created_at)]
    else:  # recent
        order_by = [desc(Contact.ultimo_mensaje_at).nullslast(), desc(Contact.created_at)]

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (
        await db.execute(
            q.order_by(*order_by).offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()
    # Etiquetas de toda la página en UNA query (antes: una por contacto).
    tags_by_contact = await _load_tags_bulk(db, [c.id for c in rows])
    items = [ContactOut.from_orm(c, tags_by_contact.get(c.id, [])) for c in rows]
    return Page(items=items, total=total, page=page, page_size=page_size)


@router.get("/stats")
async def contact_stats(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    """Agregados para la cabecera del CRM (KPIs + contadores de segmentos).

    Cuenta solo contactos dentro del CRM (`in_crm=True`), que es la población
    que se lista por defecto.
    """
    since30 = datetime.now(timezone.utc) - timedelta(days=30)
    base = Contact.in_crm.is_(True)
    total = (await db.execute(select(func.count()).select_from(Contact).where(base))).scalar_one()
    nuevos = (
        await db.execute(
            select(func.count()).select_from(Contact).where(base, Contact.created_at >= since30)
        )
    ).scalar_one()
    activos = (
        await db.execute(
            select(func.count()).select_from(Contact).where(base, Contact.ultimo_mensaje_at >= since30)
        )
    ).scalar_one()
    estado_rows = (
        await db.execute(select(Contact.estado, func.count()).where(base).group_by(Contact.estado))
    ).all()
    origen_rows = (
        await db.execute(select(Contact.origen, func.count()).where(base).group_by(Contact.origen))
    ).all()

    def _k(v: object) -> str:
        return v.value if hasattr(v, "value") else str(v)

    return {
        "total": int(total),
        "nuevos_30d": int(nuevos),
        "activos_30d": int(activos),
        "by_estado": {_k(e): int(n) for e, n in estado_rows},
        "by_origen": {_k(o): int(n) for o, n in origen_rows},
    }


# Columnas del CSV (mismas para exportar e importar). Orden estable.
# NIF, dirección, notas y etiquetas se añadieron para que el fichero sirva de
# COPIA de verdad: sin ellas, exportar e importar perdía media ficha por el
# camino sin avisar. `etiquetas` va como lista separada por `;`.
_CSV_COLUMNS = [
    "nombre", "telefono", "email", "estado", "origen", "servicio_interes",
    "empresa", "cargo", "web", "social_handle", "nif", "direccion",
    "notas_internas", "etiquetas", "created_at",
]
# Tope de filas por importación (guardia anti-abuso / memoria).
_IMPORT_MAX_ROWS = 5000
# Tope de bytes del CSV subido (DoS por memoria): se lee entero antes de filas.
_IMPORT_MAX_BYTES = 5 * 1024 * 1024  # 5 MB


_CSV_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: str) -> str:
    """Neutraliza inyección de fórmulas en CSV (Excel/LibreOffice): si el valor
    empieza por = + - @ tab o CR, lo prefijamos con comilla simple para que no se
    interprete como fórmula al abrirlo."""
    if value and value[0] in _CSV_FORMULA_STARTS:
        return "'" + value
    return value


def _csv_unsafe(value: str) -> str:
    """Deshace `_csv_safe` al LEER un CSV. La otra mitad del par.

    Sin esto, el flujo más natural del mundo —exportar, mirar el fichero en
    Excel, volver a subirlo— DUPLICABA todos los contactos con prefijo
    internacional: la exportación escribe `'+34600111222` (la comilla protege de
    la inyección de fórmulas, y todo teléfono E.164 empieza por `+`) y la
    importación comparaba esa cadena, con comilla incluida, contra el
    `+34600111222` de la base de datos. No casaban, así que creaba una ficha
    nueva por cada contacto.

    Solo se quita la comilla cuando lo que va detrás es justo uno de los
    caracteres que la habrían provocado: así un apóstrofo que forme parte del
    dato de verdad (p.ej. un nombre) se respeta.
    """
    if len(value) >= 2 and value[0] == "'" and value[1] in _CSV_FORMULA_STARTS:
        return value[1:]
    return value


@router.get("/export")
@limiter.limit(limit_spec("10/hour"))
async def export_contacts(
    request: Request,
    # Por defecto exporta LO MISMO que enseña la pantalla de Contactos: los que
    # están en el CRM. Antes sacaba también los que no lo están (visitantes de
    # Instagram sin promover), así que el fichero traía más filas de las que la
    # pantalla decía tener y no había manera de saber por qué.
    include_outside_crm: Annotated[
        bool, Query(description="Incluir también contactos que aún no están en el CRM.")
    ] = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> Response:
    """Exporta los contactos del CRM a CSV (compatible con Excel).

    Devuelve el fichero como descarga. Usa BOM utf-8 para que Excel muestre bien
    los acentos. Ojo a la ruta: va ANTES de `/{contact_id}` para que "export" no
    se interprete como un id.

    SOLO admin: esto es la base de datos de clientes entera (nombre, teléfono,
    email, empresa) en un fichero. Un operador de bandeja atiende conversaciones;
    llevarse la base no es atender conversaciones. Además va limitado por IP
    (10/hora): descargarla es una acción puntual, no un bucle.
    """
    q = select(Contact)
    if not include_outside_crm:
        q = q.where(Contact.in_crm.is_(True))
    rows = (await db.execute(q.order_by(desc(Contact.created_at)))).scalars().all()
    # Etiquetas de todos los contactos en UNA query (no una por fila).
    tags_by_contact = await _load_tags_bulk(db, [c.id for c in rows])
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_CSV_COLUMNS)
    for c in rows:
        writer.writerow([
            _csv_safe(c.nombre or ""),
            _csv_safe(c.telefono or ""),
            _csv_safe(c.email or ""),
            c.estado.value if c.estado else "",
            c.origen.value if c.origen else "",
            _csv_safe(c.servicio_interes or ""),
            _csv_safe(c.empresa or ""),
            _csv_safe(c.cargo or ""),
            _csv_safe(c.web or ""),
            _csv_safe(c.social_handle or ""),
            _csv_safe(c.nif or ""),
            _csv_safe(c.direccion or ""),
            _csv_safe(c.notas_internas or ""),
            _csv_safe(";".join(t.nombre for t in tags_by_contact.get(c.id, []))),
            c.created_at.isoformat() if c.created_at else "",
        ])
    # BOM para Excel + nombre con fecha.
    data = ("﻿" + buf.getvalue()).encode("utf-8")
    fname = f"contactos_{datetime.now(timezone.utc):%Y%m%d}.csv"
    # Auditoría: la exportación vuelca TODA la PII de contactos → dejamos rastro.
    try:
        from app.services.audit import record_audit
        await record_audit(
            db, user_id=current_user.id, action="contacts.exported",
            entity="contact", entity_id=None, after={"count": len(rows)},
        )
        await db.commit()
    except Exception:
        pass
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


class ImportResult(BaseModel):
    created: int
    updated: int
    skipped: int
    errors: list[str]


# Formato de email de la importación. El alta manual valida con EmailStr
# (pydantic); la importación no validaba NADA, así que un "juan@" o un
# "sin arroba" entraban tal cual y luego reventaban al mandarle un correo.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.post("/import", response_model=ImportResult)
@limiter.limit(limit_spec("10/hour"))
async def import_contacts(
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> ImportResult:
    """Importa contactos desde CSV (Excel → 'Guardar como CSV').

    SOLO admin y 10/hora por IP: es una escritura MASIVA (crea o pisa hasta
    _IMPORT_MAX_ROWS fichas de una tacada) y deshacerla a mano es inviable.

    - Clave de deduplicado: TELÉFONO **normalizado a E.164**. Antes se comparaba
      la cadena EXACTA, así que el CSV que produce la propia exportación no
      casaba con la base (la protección anti-fórmulas de Excel le antepone una
      comilla a todo lo que empieza por `+`, y todo teléfono E.164 empieza por
      `+`): bajarte el fichero y volver a subirlo duplicaba todos los contactos
      internacionales. Ahora cada celda se des-escapa (`_csv_unsafe`) y se
      normaliza antes de comparar.
    - Si ya existe, ACTUALIZA (sin pisar con celdas vacías); si no, CREA
      (origen=manual por defecto).
    - Columnas reconocidas: nombre, telefono, email, estado, origen,
      servicio_interes, empresa, cargo, web, social_handle, nif, direccion,
      notas_internas. Solo `telefono` es obligatorio.
    - `estado`/`origen` inválidos se ignoran (se usa el valor por defecto).
    - El email se VALIDA (igual que en el alta manual): una fila con un email
      con mala pinta se rechaza diciendo por qué, no se cuela sin más.
    - Todo lo que crea o pisa queda registrado en la actividad del contacto y en
      la auditoría.
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Archivo vacío")
    if len(raw) > _IMPORT_MAX_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"El archivo supera el máximo permitido ({_IMPORT_MAX_BYTES // (1024 * 1024)} MB).",
        )
    # utf-8-sig quita el BOM de Excel; fallback a latin-1 por si acaso.
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "telefono" not in [(f or "").strip().lower() for f in reader.fieldnames]:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "El CSV debe tener una columna 'telefono'. Exporta primero para ver el formato.",
        )

    valid_estados = {e.value for e in ContactEstado}
    valid_origenes = {o.value for o in ContactOrigen}

    created = updated = skipped = 0
    errors: list[str] = []

    # Primera pasada: leer y limpiar el fichero entero en memoria. Así podemos
    # resolver los teléfonos ya existentes en UNA sola consulta en vez de una
    # por fila (5.000 filas eran 5.000 SELECTs).
    parsed: list[tuple[int, dict[str, str], str]] = []  # (nº fila, celdas, teléfono)
    for i, row in enumerate(reader, start=2):  # fila 1 = cabecera
        if len(parsed) + skipped >= _IMPORT_MAX_ROWS:
            errors.append(f"Cortado en {_IMPORT_MAX_ROWS} filas; el resto no se importó.")
            break
        # Normaliza claves (minúsculas, sin espacios) y des-escapa la protección
        # anti-fórmulas que puso la exportación.
        r = {
            (k or "").strip().lower(): _csv_unsafe((v or "").strip())
            for k, v in row.items()
            if k is not None
        }
        telefono_raw = r.get("telefono", "")
        if not telefono_raw:
            skipped += 1
            errors.append(f"Fila {i}: sin teléfono (columna 'telefono' vacía).")
            continue
        # E.164: "+34 600 111 222", "0034600111222" y "'+34600111222" acaban
        # todos en la MISMA ficha.
        telefono = normalize_phone(telefono_raw)
        if not telefono:
            skipped += 1
            errors.append(f"Fila {i}: teléfono no válido ({telefono_raw[:40]}).")
            continue
        email = (r.get("email") or "").lower() or None
        if email and not _EMAIL_RE.match(email):
            skipped += 1
            errors.append(f"Fila {i}: email no válido ({email[:60]}).")
            continue
        r["telefono"] = telefono
        r["email"] = email or ""
        parsed.append((i, r, telefono))

    # Los que ya existen, de una tacada.
    existing_by_phone: dict[str, Contact] = {}
    phones = list({p for _, _, p in parsed})
    if phones:
        for c in (
            await db.execute(select(Contact).where(Contact.telefono.in_(phones)))
        ).scalars().all():
            existing_by_phone[c.telefono] = c

    # Ficha del contacto que se crea/pisa, para dejar rastro en su actividad.
    touched: list[tuple[uuid.UUID | None, Contact, bool]] = []

    for i, r, telefono in parsed:
        nombre = r.get("nombre") or None
        email = r.get("email") or None
        servicio = r.get("servicio_interes") or None
        empresa = r.get("empresa") or None
        cargo = r.get("cargo") or None
        web = r.get("web") or None
        handle = r.get("social_handle") or None
        nif = r.get("nif") or None
        direccion = r.get("direccion") or None
        notas = r.get("notas_internas") or None
        estado_raw = r.get("estado") or ""
        estado = estado_raw if estado_raw in valid_estados else None
        origen_raw = r.get("origen") or ""
        origen = origen_raw if origen_raw in valid_origenes else None

        try:
            existing = existing_by_phone.get(telefono)
            if existing:
                # Actualiza solo lo que viene con valor (no pisa con vacío).
                if nombre:
                    existing.nombre = nombre
                if email:
                    existing.email = email
                if servicio:
                    existing.servicio_interes = servicio
                if empresa:
                    existing.empresa = empresa
                if cargo:
                    existing.cargo = cargo
                if web:
                    existing.web = web
                if handle:
                    existing.social_handle = handle
                if nif:
                    existing.nif = nif
                if direccion:
                    existing.direccion = direccion
                if notas:
                    existing.notas_internas = notas
                if estado:
                    existing.estado = ContactEstado(estado)
                updated += 1
                touched.append((existing.id, existing, False))
            else:
                nuevo = Contact(
                    telefono=telefono,
                    email=email,
                    nombre=nombre,
                    estado=ContactEstado(estado) if estado else ContactEstado.contacto,
                    origen=ContactOrigen(origen) if origen else ContactOrigen.manual,
                    servicio_interes=servicio,
                    empresa=empresa,
                    cargo=cargo,
                    web=web,
                    social_handle=handle,
                    nif=nif,
                    direccion=direccion,
                    notas_internas=notas,
                )
                db.add(nuevo)
                # El mismo teléfono repetido dentro del propio CSV: la segunda
                # vez actualiza la ficha recién creada en vez de crear otra.
                existing_by_phone[telefono] = nuevo
                created += 1
                touched.append((None, nuevo, True))
        except Exception as e:  # noqa: BLE001 — una fila mala no aborta el resto
            skipped += 1
            if len(errors) < 20:
                errors.append(f"Fila {i}: {str(e)[:120]}")

    # Rastro en la ficha de cada contacto tocado: una importación creaba y
    # pisaba fichas sin dejar ni una línea en su actividad, así que al mirar un
    # contacto no había forma de saber de dónde había salido ese cambio.
    if touched:
        await db.flush()  # asigna id a los recién creados
        for _prev_id, contact_obj, is_new in touched:
            await record_activity(
                db,
                contact_obj.id,
                "contact_created" if is_new else "contact_imported",
                actor_user_id=current_user.id,
                meta={"fuente": "import_csv"},
            )

    # Escritura masiva: queda registrada igual que la exportación.
    try:
        from app.services.audit import record_audit
        await record_audit(
            db, user_id=current_user.id, action="contacts.imported",
            entity="contact", entity_id=None,
            after={"created": created, "updated": updated, "skipped": skipped},
        )
    except Exception:
        pass
    await db.commit()
    # Solo los 20 primeros errores viajan: con un fichero roto entero, la lista
    # completa no cabe en la pantalla ni aporta nada.
    if len(errors) > 20:
        restantes = len(errors) - 20
        errors = errors[:20] + [f"…y {restantes} fila(s) más con problemas."]
    return ImportResult(created=created, updated=updated, skipped=skipped, errors=errors)


@router.get("/{contact_id}")
async def get_contact(
    contact_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> ContactOut:
    contact = (await db.execute(select(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
    if not contact:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    tags = await _load_tags(db, contact.id)
    return ContactOut.from_orm(contact, tags)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_contact(
    payload: ContactCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ContactOut:
    email = payload.email.lower() if payload.email else None
    # Teléfono a E.164 en el ALTA (antes se guardaba tal cual lo escribiera
    # quien fuera: "600 111 222" abría una ficha distinta de "+34600111222" y
    # los mensajes entrantes de ese cliente no la encontraban nunca).
    telefono = normalize_phone(payload.telefono or "")
    if not telefono:
        if not email:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Hace falta un teléfono o un email para crear el contacto",
            )
        telefono = f"email:{email}"
    existing = (
        await db.execute(select(Contact).where(Contact.telefono == telefono))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Ya existe un contacto con ese teléfono"
            if not telefono.startswith("email:")
            else "Ya existe un contacto con ese email",
        )
    contact = Contact(
        telefono=telefono,
        email=email,
        nombre=payload.nombre,
        estado=payload.estado,
        origen=ContactOrigen.manual,
        servicio_interes=payload.servicio_interes,
        empresa=payload.empresa,
        cargo=payload.cargo,
        web=payload.web,
        nif=payload.nif,
        direccion=payload.direccion,
        notas_internas=payload.notas_internas,
    )
    db.add(contact)
    # Necesitamos el id del contacto para el evento; flush sin commit.
    await db.flush()
    # Auditoría: contacto creado (actor = usuario actual). Mismo commit.
    await record_activity(
        db,
        contact.id,
        "contact_created",
        actor_user_id=current_user.id,
        meta={"origen": contact.origen.value},
    )
    await db.commit()
    await db.refresh(contact)
    return ContactOut.from_orm(contact)


@router.patch("/{contact_id}")
async def update_contact(
    contact_id: uuid.UUID,
    payload: ContactUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ContactOut:
    contact = (await db.execute(select(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
    if not contact:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    data = payload.model_dump(exclude_unset=True)
    if "email" in data and data["email"]:
        data["email"] = data["email"].lower()
    # ── Cambio de teléfono ────────────────────────────────────────────────
    # El teléfono es la LLAVE del contacto: por ahí se emparejan los mensajes
    # entrantes con su ficha. Se puede corregir (antes había que borrar la ficha
    # y perder conversaciones, notas y actividad), pero con tres frenos.
    telefono_prev = contact.telefono
    telefono_cambia = False
    if "telefono" in data:
        nuevo = normalize_phone(data.pop("telefono") or "")
        if not nuevo:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "El teléfono no puede quedar vacío"
            )
        if nuevo != telefono_prev:
            # 1) Los identificadores de canal (wa:, ig:, web:, email:, voice:)
            #    NO son teléfonos: son la única llave que tiene ese contacto con
            #    su canal. Cambiarlos deja la ficha huérfana de sus mensajes
            #    futuros y no hay forma de recuperarla.
            if is_channel_identifier(telefono_prev):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Este contacto se identifica por su canal (web, Instagram, email o voz), "
                    "no por un teléfono. Cambiarlo lo desconectaría de sus conversaciones.",
                )
            if is_channel_identifier(nuevo):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "Eso no es un teléfono. Escribe el número en formato internacional (+34600111222).",
                )
            # 2) Que el número nuevo no sea ya de otra ficha: si lo es, lo que
            #    quiere hacerse es una FUSIÓN, y para eso está /merge.
            choque = (
                await db.execute(select(Contact).where(Contact.telefono == nuevo))
            ).scalar_one_or_none()
            if choque:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Ya hay otro contacto con ese teléfono. Si son la misma persona, fusiona las dos fichas.",
                )
            contact.telefono = nuevo
            telefono_cambia = True
    # Capturamos el estado previo antes de aplicar cambios, para detectar si
    # el estado cambia DE VERDAD (no registramos un no-op).
    estado_prev = contact.estado
    for k, v in data.items():
        setattr(contact, k, v)
    # 3) Rastro: cambiar la llave de un contacto con conversaciones no es un
    #    detalle cosmético. Se registra (SIN el número: es PII) junto con
    #    cuántas conversaciones tenía cuando se cambió.
    if telefono_cambia:
        from app.models.conversation import Conversation

        n_convs = (
            await db.execute(
                select(func.count())
                .select_from(Conversation)
                .where(Conversation.contact_id == contact.id)
            )
        ).scalar_one()
        await record_activity(
            db,
            contact.id,
            "phone_changed",
            actor_user_id=current_user.id,
            meta={"conversaciones_afectadas": int(n_convs or 0)},
        )
    # Auditoría: solo si el estado cambió realmente. meta lleva los valores de
    # estado (no PII), de → a. Mismo commit que la mutación.
    if "estado" in data and data["estado"] is not None and contact.estado != estado_prev:
        await record_activity(
            db,
            contact.id,
            "status_changed",
            actor_user_id=current_user.id,
            meta={
                "from": estado_prev.value if hasattr(estado_prev, "value") else str(estado_prev),
                "to": contact.estado.value if hasattr(contact.estado, "value") else str(contact.estado),
            },
        )
    await db.commit()
    await db.refresh(contact)
    tags = await _load_tags(db, contact.id)
    return ContactOut.from_orm(contact, tags)


class ContactMergeBody(BaseModel):
    # Ficha que se absorbe y desaparece. La que sobrevive es la de la URL.
    source_id: uuid.UUID


@router.post("/{contact_id}/merge", response_model=ContactOut)
async def merge_contacts(
    contact_id: uuid.UUID,
    payload: ContactMergeBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> ContactOut:
    """Fusiona dos fichas del mismo cliente en una.

    El mismo cliente acaba con varias fichas más a menudo de lo que parece:
    escribe por WhatsApp desde `+34600111222`, rellena un formulario poniendo
    `600 111 222` y alguien lo importa de un Excel como `34600111222`. Hasta
    ahora convivían como tres personas distintas y no había manera de unirlas:
    lo único posible era borrar dos, y borrar se lleva por delante sus
    conversaciones, sus notas y su actividad.

    Qué hace, en una sola transacción:
      - mueve conversaciones, notas y actividad de `source_id` a `contact_id`
        (los mensajes viajan con su conversación);
      - copia las etiquetas que le faltaran al destino;
      - rellena los HUECOS del destino con lo que tuviera el origen (nombre,
        email, empresa…). Lo que el destino ya tenía NO se pisa: fusionar no
        puede empeorar la ficha buena;
      - se queda con la fecha de alta más antigua y la actividad más reciente;
      - borra la ficha de origen y deja rastro en las dos (auditoría + una
        entrada en la actividad del contacto que sobrevive).

    SOLO admin: es una operación irreversible sobre datos de clientes, del mismo
    calibre que el borrado.
    """
    from app.services.audit import record_audit
    from app.services.contact_merge import fusionar_contactos

    if contact_id == payload.source_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "No se puede fusionar un contacto consigo mismo",
        )
    destino = (
        await db.execute(select(Contact).where(Contact.id == contact_id))
    ).scalar_one_or_none()
    if not destino:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contacto destino no encontrado")
    origen = (
        await db.execute(select(Contact).where(Contact.id == payload.source_id))
    ).scalar_one_or_none()
    if not origen:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contacto origen no encontrado")

    telefono_origen = origen.telefono

    # La fusión de verdad vive en `services/contact_merge.py`: la comparte con
    # la ENTRADA de mensajes, que desde el BSUID también fusiona sola cuando
    # descubre que dos fichas son la misma persona. Dos copias de una operación
    # irreversible sobre datos de clientes acabarían divergiendo.
    resumen = await fusionar_contactos(db, destino, origen)

    # La actividad del contacto que sobrevive cuenta que hubo fusión. `meta` NO
    # lleva PII: ni el teléfono ni el nombre de la ficha absorbida.
    await record_activity(
        db,
        destino.id,
        "contacts_merged",
        actor_user_id=current_user.id,
        meta=resumen,
    )
    # La auditoría sí guarda el id de la ficha que desapareció, que es el único
    # sitio donde va a quedar constancia de que existió.
    await record_audit(
        db,
        user_id=current_user.id,
        action="contacts.merged",
        entity="contact",
        entity_id=destino.id,
        before={"source_id": str(payload.source_id), "source_era_identificador_de_canal": is_channel_identifier(telefono_origen)},
        after=resumen,
    )
    await db.commit()
    await db.refresh(destino)
    tags = await _load_tags(db, destino.id)
    return ContactOut.from_orm(destino, tags)


@router.delete("/{contact_id}", response_model=OkResponse)
async def delete_contact(
    contact_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> OkResponse:
    """Borrado RGPD de un contacto. SOLO admin y siempre auditado.

    Antes bastaba con estar identificado: cualquier usuario con rol `cliente`
    podía llevarse por delante el contacto, sus conversaciones, sus mensajes y
    sus ficheros — es irreversible y no quedaba ni una línea de quién lo hizo.
    """
    from app.services import data_erasure
    from app.services.audit import record_audit

    contact = (await db.execute(select(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
    if not contact:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    # Borrado COMPLETO (RGPD Art. 17): además del cascade de BD, elimina
    # ficheros de audio/media en disco, huecos de conocimiento derivados de sus
    # mensajes, destinatarios de envíos masivos con su teléfono y claves de
    # Redis. Antes era un db.delete simple que dejaba PII huérfana.
    report = await data_erasure.erase_contact(db, contact_id)
    # Auditoría: mismo commit que el borrado. El informe dice qué se llevó por
    # delante (contadores, sin PII) y, si algo NO se pudo borrar (la grabación
    # de la llamada vive en Retell), lo dice en vez de callárselo.
    await record_audit(
        db,
        user_id=current_user.id,
        action="contact.data_erased",
        entity="contact",
        entity_id=contact_id,
        after={k: v for k, v in report.items() if not k.startswith("_")},
    )
    await db.commit()
    # Limpieza de Redis DESPUÉS del commit: si el commit hubiera fallado, no
    # habríamos dejado a medias lo que no participa en la transacción.
    await data_erasure.finish_erasure(report)
    return OkResponse()


class ConversationMini(BaseModel):
    id: uuid.UUID
    canal: str
    status: str
    started_at: str
    last_message_at: str | None
    archived: bool
    # Vista previa (texto truncado) del último mensaje del hilo. Misma lógica
    # que el listado del inbox: NO es dato nuevo, sólo expone el mensaje ya
    # existente para presentar las conversaciones de la ficha con contexto.
    last_message_preview: str | None = None


@router.get("/{contact_id}/conversations", response_model=list[ConversationMini])
async def list_contact_conversations(
    contact_id: uuid.UUID,
    # Tope de hilos. Antes se traían TODOS y, encima, con un SELECT del último
    # mensaje por cada uno: un cliente con dos años de histórico abría su ficha
    # con cientos de consultas. 100 hilos es más de lo que nadie mira de un
    # vistazo; quien quiera más los tiene en la bandeja.
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[ConversationMini]:
    from app.models.conversation import Conversation
    from app.models.message import Message
    rows = (
        await db.execute(
            select(Conversation)
            .where(Conversation.contact_id == contact_id)
            .order_by(desc(Conversation.started_at))
            .limit(limit)
        )
    ).scalars().all()

    # Vista previa del último mensaje de CADA hilo en UNA sola query
    # (DISTINCT ON, mismo patrón que la lista del inbox) en vez de una por hilo.
    previews: dict[uuid.UUID, str] = {}
    conv_ids = [c.id for c in rows]
    if conv_ids:
        last_rows = (
            await db.execute(
                select(Message)
                .where(Message.conversation_id.in_(conv_ids))
                .distinct(Message.conversation_id)
                .order_by(Message.conversation_id, desc(Message.created_at))
            )
        ).scalars().all()
        for m in last_rows:
            if (m.extra or {}).get("purged"):
                p = "[correo archivado]"
            else:
                p = m.contenido or m.audio_transcript or "[audio]"
            if p and len(p) > 80:
                p = p[:77] + "…"
            previews[m.conversation_id] = p

    out: list[ConversationMini] = []
    for c in rows:
        preview = previews.get(c.id)
        out.append(
            ConversationMini(
                id=c.id,
                canal=c.canal.value,
                status=c.status.value,
                started_at=c.started_at.isoformat(),
                last_message_at=c.last_message_at.isoformat() if c.last_message_at else None,
                archived=c.archived_at is not None,
                last_message_preview=preview,
            )
        )
    return out


async def _get_contact_or_404(db: AsyncSession, contact_id: uuid.UUID) -> Contact:
    contact = (
        await db.execute(select(Contact).where(Contact.id == contact_id))
    ).scalar_one_or_none()
    if not contact:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contacto no encontrado")
    return contact


@router.get("/{contact_id}/notes", response_model=list[ContactNoteOut])
async def list_contact_notes(
    contact_id: uuid.UUID,
    # Tope: se devolvían TODAS. Una ficha muy trabajada acababa mandando cientos
    # de notas descifradas en cada apertura.
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[ContactNoteOut]:
    """Notas internas del contacto, más recientes primero, con autor resuelto."""
    await _get_contact_or_404(db, contact_id)
    rows = (
        await db.execute(
            select(ContactNote, User)
            .outerjoin(User, User.id == ContactNote.author_user_id)
            .where(ContactNote.contact_id == contact_id)
            .order_by(desc(ContactNote.created_at))
            .limit(limit)
        )
    ).all()
    return [ContactNoteOut.from_orm(note, author) for note, author in rows]


@router.post("/{contact_id}/notes", status_code=status.HTTP_201_CREATED, response_model=ContactNoteOut)
async def create_contact_note(
    contact_id: uuid.UUID,
    payload: ContactNoteCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ContactNoteOut:
    """Crea una nota cuyo autor es el usuario autenticado."""
    await _get_contact_or_404(db, contact_id)
    texto = payload.texto.strip()
    if not texto:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "El texto no puede estar vacío")
    note = ContactNote(contact_id=contact_id, author_user_id=current_user.id, texto=texto)
    db.add(note)
    # Auditoría: nota añadida (actor = usuario). NUNCA el texto en meta (PII).
    await record_activity(
        db, contact_id, "note_added", actor_user_id=current_user.id
    )
    await db.commit()
    await db.refresh(note)
    # El autor es el usuario actual (lo acabamos de asignar).
    return ContactNoteOut.from_orm(note, current_user)


@router.delete("/{contact_id}/notes/{note_id}", response_model=OkResponse)
async def delete_contact_note(
    contact_id: uuid.UUID,
    note_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> OkResponse:
    """Borra una nota. Permitido al autor de la nota o a un admin."""
    note = (
        await db.execute(
            select(ContactNote).where(
                ContactNote.id == note_id, ContactNote.contact_id == contact_id
            )
        )
    ).scalar_one_or_none()
    if not note:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Nota no encontrada")
    is_author = note.author_user_id is not None and note.author_user_id == current_user.id
    is_admin = current_user.role == UserRole.admin
    if not (is_author or is_admin):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Solo el autor o un admin puede borrar la nota")
    autor_original = note.author_user_id
    await db.delete(note)
    # La línea de tiempo de la ficha registraba "nota añadida" pero NO "nota
    # borrada": una nota podía desaparecer sin que quedara ni rastro en el sitio
    # donde se mira. Como en el resto, meta NUNCA lleva el texto (es PII).
    await record_activity(
        db,
        contact_id,
        "note_deleted",
        actor_user_id=current_user.id,
        meta={"borrada_por_el_autor": is_author},
    )
    # Rastro de quién la borró: un admin puede borrar la nota de otro y hasta
    # ahora eso no dejaba ninguna huella. NUNCA el texto de la nota (es PII);
    # sí quién la escribió y quién la borró.
    from app.services.audit import record_audit

    await record_audit(
        db,
        user_id=current_user.id,
        action="contact.note_deleted",
        entity="contact_note",
        entity_id=note_id,
        before={"contact_id": str(contact_id), "author_user_id": str(autor_original) if autor_original else None},
        after={"borrada_por_el_autor": is_author},
    )
    await db.commit()
    return OkResponse()


@router.get("/{contact_id}/activity", response_model=list[ContactActivityOut])
async def list_contact_activity(
    contact_id: uuid.UUID,
    # Tope: la línea de tiempo crece sin fin (cada etiqueta, cada cambio de
    # estado, cada conversación). Se devolvía entera en cada apertura de ficha.
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[ContactActivityOut]:
    """Timeline de actividad del contacto, más recientes primero.

    Cada evento sale con su actor resuelto (outerjoin a User → {id, nombre,
    role}) o null si fue el sistema/agente o el usuario ya no existe.
    """
    await _get_contact_or_404(db, contact_id)
    rows = (
        await db.execute(
            select(ContactActivity, User)
            .outerjoin(User, User.id == ContactActivity.actor_user_id)
            .where(ContactActivity.contact_id == contact_id)
            .order_by(desc(ContactActivity.created_at))
            .limit(limit)
        )
    ).all()
    return [ContactActivityOut.from_orm(ev, actor) for ev, actor in rows]


async def _tag_nombre(db: AsyncSession, tag_id: uuid.UUID) -> str | None:
    """Resuelve el nombre de una etiqueta para el meta del evento (no PII)."""
    return (
        await db.execute(select(Tag.nombre).where(Tag.id == tag_id))
    ).scalar_one_or_none()


@router.post("/{contact_id}/tags/{tag_id}", response_model=OkResponse)
async def add_tag(
    contact_id: uuid.UUID,
    tag_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> OkResponse:
    exists = (
        await db.execute(
            select(ContactTag).where(ContactTag.contact_id == contact_id, ContactTag.tag_id == tag_id)
        )
    ).scalar_one_or_none()
    if not exists:
        db.add(ContactTag(contact_id=contact_id, tag_id=tag_id))
        # Auditoría: etiqueta añadida. meta lleva el NOMBRE de la etiqueta (no
        # es PII). Solo registramos cuando se añade de verdad (idempotente).
        await record_activity(
            db,
            contact_id,
            "tag_added",
            actor_user_id=current_user.id,
            meta={"tag": await _tag_nombre(db, tag_id)},
        )
        await db.commit()
    return OkResponse()


@router.delete("/{contact_id}/tags/{tag_id}", response_model=OkResponse)
async def remove_tag(
    contact_id: uuid.UUID,
    tag_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> OkResponse:
    link = (
        await db.execute(
            select(ContactTag).where(ContactTag.contact_id == contact_id, ContactTag.tag_id == tag_id)
        )
    ).scalar_one_or_none()
    if link:
        # Resolvemos el nombre ANTES de borrar el vínculo (la etiqueta en sí no
        # se borra, pero lo resolvemos aquí por simetría con add_tag).
        nombre = await _tag_nombre(db, tag_id)
        await db.delete(link)
        # Auditoría: etiqueta quitada. meta lleva el NOMBRE (no PII).
        await record_activity(
            db,
            contact_id,
            "tag_removed",
            actor_user_id=current_user.id,
            meta={"tag": nombre},
        )
        await db.commit()
    return OkResponse()


@router.post("/{contact_id}/add-to-crm", response_model=ContactOut)
async def add_contact_to_crm(
    contact_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> ContactOut:
    """Promueve un contacto al CRM (lo hace aparecer en /contacts).

    Pensado para visitors de Instagram que el operador decide convertir
    en lead/contacto real. Idempotente: si ya está en el CRM, no error.
    """
    contact = (
        await db.execute(select(Contact).where(Contact.id == contact_id))
    ).scalar_one_or_none()
    if not contact:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contacto no encontrado")
    if not contact.in_crm:
        contact.in_crm = True
        await db.commit()
        await db.refresh(contact)
    tags = await _load_tags(db, contact.id)
    return ContactOut.from_orm(contact, tags)


@router.get("/{contact_id}/export")
@limiter.limit(limit_spec("30/hour"))
async def export_contact_data(
    request: Request,
    contact_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> Response:
    """RGPD Art. 15/20 — exporta TODOS los datos de un contacto en JSON legible
    (derecho de acceso/portabilidad): ficha, conversaciones con sus mensajes
    (texto y transcripciones), notas y línea de actividad. Descarga directa.

    Nota: los ficheros de audio no van en el JSON (pueden pesar mucho); el
    export indica qué mensajes tenían audio.

    SOLO admin: es el otro lado del derecho de supresión (que ya era de admin).
    Responder a una solicitud RGPD es una obligación del responsable del
    tratamiento; un operador de bandeja no la atiende, y con este volcado se
    lleva el histórico completo y las transcripciones de un cliente en un
    fichero. Limitado a 30/hora por IP para que no sirva de exportador masivo
    contacto a contacto.
    """
    import json as _json

    from app.models.contact_activity import ContactActivity
    from app.models.contact_note import ContactNote
    from app.models.conversation import Conversation
    from app.models.message import Message
    from app.services.audit import record_audit

    contact = (
        await db.execute(select(Contact).where(Contact.id == contact_id))
    ).scalar_one_or_none()
    if not contact:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contacto no encontrado")

    tags = await _load_tags(db, contact.id)
    convs = (
        await db.execute(
            select(Conversation)
            .where(Conversation.contact_id == contact_id)
            .order_by(Conversation.started_at)
        )
    ).scalars().all()
    conv_ids = [c.id for c in convs]
    msgs_by_conv: dict = {}
    if conv_ids:
        msgs = (
            await db.execute(
                select(Message)
                .where(Message.conversation_id.in_(conv_ids))
                .order_by(Message.created_at)
            )
        ).scalars().all()
        for m in msgs:
            msgs_by_conv.setdefault(m.conversation_id, []).append(m)
    notes = (
        await db.execute(
            select(ContactNote)
            .where(ContactNote.contact_id == contact_id)
            .order_by(ContactNote.created_at)
        )
    ).scalars().all()
    activity = (
        await db.execute(
            select(ContactActivity)
            .where(ContactActivity.contact_id == contact_id)
            .order_by(ContactActivity.created_at)
        )
    ).scalars().all()

    def _iso(dt) -> str | None:
        return dt.isoformat() if dt else None

    data = {
        "_export": {
            "generado": datetime.now(timezone.utc).isoformat(),
            "descripcion": "Exportación de datos personales (RGPD Art. 15/20)",
        },
        "contacto": {
            "nombre": contact.nombre,
            "telefono": contact.telefono,
            "email": contact.email,
            "usuario_instagram": contact.social_handle,
            "estado": contact.estado.value if contact.estado else None,
            "origen": contact.origen.value if contact.origen else None,
            "servicio_interes": contact.servicio_interes,
            "empresa": contact.empresa,
            "cargo": contact.cargo,
            "web": contact.web,
            "nif": contact.nif,
            "direccion": contact.direccion,
            "notas_internas": contact.notas_internas,
            "etiquetas": [t.nombre for t in tags],
            "creado": _iso(contact.created_at),
        },
        "conversaciones": [
            {
                "canal": c.canal.value,
                "estado": c.status.value,
                "inicio": _iso(c.started_at),
                "ultimo_mensaje": _iso(c.last_message_at),
                "resumen": c.resumen,
                "mensajes": [
                    {
                        "fecha": _iso(m.created_at),
                        "de": "cliente" if m.rol.value == "user" else "negocio",
                        "texto": m.contenido,
                        "transcripcion_audio": m.audio_transcript,
                        "tenia_audio": bool(m.audio_url or m.media_purged_at),
                        "adjunto": m.media_filename,
                    }
                    for m in msgs_by_conv.get(c.id, [])
                ],
            }
            for c in convs
        ],
        "notas": [{"fecha": _iso(n.created_at), "texto": n.texto} for n in notes],
        "actividad": [
            {"fecha": _iso(a.created_at), "tipo": a.tipo, "detalle": a.meta}
            for a in activity
        ],
    }

    await record_audit(
        db,
        user_id=current_user.id,
        action="contact.data_exported",
        entity="contact",
        entity_id=contact.id,
    )
    await db.commit()

    body = _json.dumps(data, ensure_ascii=False, indent=2, default=str)
    filename = f"datos-{(contact.nombre or 'contacto').strip().replace(' ', '_')[:40]}.json"
    return Response(
        content=body.encode("utf-8"),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
