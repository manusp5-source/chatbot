import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.core.config import settings
from app.core.ratelimit import consume_user_daily_quota, limit_spec, make_limiter
from app.db.session import get_db
from app.models.chunk import Chunk
from app.models.document import Document, DocumentFormato, DocumentStatus, DocumentVersion
from app.models.user import User
from app.providers.embeddings import embed_texts
from app.schemas.common import OkResponse

router = APIRouter(prefix="/kb", tags=["knowledge_base"])

# TODA la base de conocimiento es de ADMIN, también lo que solo lee. Antes
# bastaba con estar identificado: un rol `cliente` (operador de bandeja) podía
# volcarse la KB entera — documentos, chunks y el texto completo de cada uno —
# que es justo donde vive lo interno del negocio (precios, procedimientos,
# políticas). Y `POST /search` además GASTA: cada búsqueda es una llamada de
# pago a embeddings, así que una sesión de operador podía quemar presupuesto en
# bucle. Escribir ya era de admin; ahora leer y buscar también.

limiter = make_limiter()

# Freno por IP de la búsqueda semántica (lo único de la KB que cuesta dinero por
# petición). Se suma al cupo diario por usuario de abajo: uno corta la ráfaga,
# el otro el goteo constante.
KB_SEARCH_DAILY_LIMIT_PER_USER = int(os.getenv("KB_SEARCH_DAILY_LIMIT_PER_USER", "300"))

# KB_STORAGE tiene que estar DENTRO del volumen Docker compartido entre
# `app` (sube el archivo) y `worker` (lo indexa). El volumen `audios_data`
# se monta en `/data/audios` en ambos servicios, asi que usamos un subdir
# `/data/audios/kb_docs`. Antes apuntaba a `/data/kb_docs` (fuera del volumen)
# y el worker no encontraba los archivos que `app` acababa de escribir.
KB_STORAGE = os.path.join(settings.AUDIO_STORAGE_PATH, "kb_docs")
os.makedirs(KB_STORAGE, exist_ok=True)
KB_STORAGE_ROOT = Path(KB_STORAGE).resolve()

MAX_FILE_BYTES = 25 * 1024 * 1024  # 25 MB

# Tope de la edición/creación de texto desde el panel. Un documento de texto de
# la KB razonable son unos pocos KB; 200k caracteres da margen de sobra sin
# permitir payloads absurdos en un JSON.
MAX_TEXT_CHARS = 200_000
# Versiones que conservamos por documento (las más antiguas se recortan).
MAX_VERSIONS_PER_DOC = 20

# Solo los formatos de texto plano se editan desde el panel; un PDF/DOCX/XLSX
# se reemplaza subiendo el archivo nuevo.
EDITABLE_FORMATS = {DocumentFormato.txt, DocumentFormato.md}

EXT_TO_FORMATO = {
    ".pdf": DocumentFormato.pdf,
    ".docx": DocumentFormato.docx,
    ".txt": DocumentFormato.txt,
    ".md": DocumentFormato.md,
    ".csv": DocumentFormato.csv,
    ".xlsx": DocumentFormato.xlsx,
}

# Firmas (magic bytes) por extensión. Para txt/md/csv se valida por decodificación.
_MAGIC_BYTES: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    ".docx": (b"PK\x03\x04",),
    ".xlsx": (b"PK\x03\x04",),
}


def _is_valid_content(ext: str, content: bytes) -> bool:
    sigs = _MAGIC_BYTES.get(ext)
    if sigs:
        return any(content.startswith(sig) for sig in sigs)
    if ext in {".txt", ".md", ".csv"}:
        try:
            content.decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False
    return True


class DocumentOut(BaseModel):
    id: uuid.UUID
    nombre: str
    formato: str
    tamano_bytes: int
    status: str
    error_msg: str | None
    num_chunks: int
    uploaded_at: str

    class Config:
        from_attributes = True


class KBSearchRequest(BaseModel):
    query: str
    # Acotado: top_k va directo al LIMIT de match_chunks; sin tope, un valor
    # enorme (o negativo) llegaba crudo a la query → 422 automático fuera de rango.
    top_k: int = Field(5, ge=1, le=8)


class KBSearchResult(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    contenido: str
    similarity: float
    metadata: dict | None = None


@router.get("/documents")
async def list_documents(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[DocumentOut]:
    docs = (
        await db.execute(select(Document).order_by(desc(Document.uploaded_at)))
    ).scalars().all()
    return [
        DocumentOut(
            id=d.id,
            nombre=d.nombre,
            formato=d.formato.value,
            tamano_bytes=d.tamano_bytes,
            status=d.status.value,
            error_msg=d.error_msg,
            num_chunks=d.num_chunks,
            uploaded_at=d.uploaded_at.isoformat(),
        )
        for d in docs
    ]


@router.post("/documents")
async def upload_document(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> DocumentOut:
    if not file.filename:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Archivo sin nombre")
    # Sanitiza el nombre: solo el basename, sin paths. Limitado en longitud.
    safe_name = os.path.basename(file.filename)[:200]
    ext = Path(safe_name).suffix.lower()
    formato = EXT_TO_FORMATO.get(ext)
    if not formato:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Formato no soportado: {ext}. Aceptados: pdf, docx, txt, md, csv, xlsx",
        )

    content = await file.read()
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Archivo demasiado grande (max 25 MB)")

    if not _is_valid_content(ext, content):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "El contenido no coincide con la extensión declarada",
        )

    doc_id = uuid.uuid4()
    # Path seguro bajo KB_STORAGE_ROOT (uuid sin componentes de path).
    storage_path = str((KB_STORAGE_ROOT / f"{doc_id}{ext}").resolve())
    if not storage_path.startswith(str(KB_STORAGE_ROOT)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ruta inválida")
    with open(storage_path, "wb") as f:
        f.write(content)

    doc = Document(
        id=doc_id,
        nombre=safe_name,
        formato=formato,
        tamano_bytes=len(content),
        status=DocumentStatus.procesando,
        storage_path=storage_path,
        uploaded_by=user.id,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    # SEGUNDO PLANO, no dentro del request. La extracción de un PDF/Word/Excel de
    # hasta 25 MB es síncrona y pesada: hacerla aquí dejaba parado el event loop
    # entero mientras duraba — ni respondía el panel ni entraban los webhooks de
    # WhatsApp e Instagram. El documento se devuelve en `procesando` y la
    # pantalla refleja el resultado cuando termina.
    from app.services.kb_indexer import schedule_indexing
    await schedule_indexing(doc.id)
    await db.refresh(doc)

    return DocumentOut(
        id=doc.id,
        nombre=doc.nombre,
        formato=doc.formato.value,
        tamano_bytes=doc.tamano_bytes,
        status=doc.status.value,
        error_msg=doc.error_msg,
        num_chunks=doc.num_chunks,
        uploaded_at=doc.uploaded_at.isoformat(),
    )


class NoteIn(BaseModel):
    titulo: str = Field(..., min_length=1, max_length=200)
    contenido: str = Field(..., min_length=1, max_length=MAX_TEXT_CHARS)


async def create_text_document(
    db: AsyncSession, titulo: str, contenido: str, user_id: uuid.UUID | None
) -> Document:
    """Crea un documento .txt indexado a partir de título + texto. Lo usan la
    nota manual del panel, la propuesta aplicada del agente interno y la API
    de agentes externos — mismo camino, mismas validaciones de path."""
    titulo = " ".join((titulo or "").split()).strip()
    contenido = (contenido or "").strip()
    if not titulo or not contenido:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Título y contenido son obligatorios")

    doc_id = uuid.uuid4()
    storage_path = str((KB_STORAGE_ROOT / f"{doc_id}.txt").resolve())
    if not storage_path.startswith(str(KB_STORAGE_ROOT)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ruta inválida")
    raw = contenido.encode("utf-8")
    with open(storage_path, "wb") as f:
        f.write(raw)

    nombre = titulo[:250]
    if not nombre.lower().endswith((".txt", ".md")):
        nombre = f"{nombre}.txt"
    doc = Document(
        id=doc_id,
        nombre=nombre,
        formato=DocumentFormato.txt,
        tamano_bytes=len(raw),
        status=DocumentStatus.procesando,
        storage_path=storage_path,
        uploaded_by=user_id,
    )
    db.add(doc)
    await db.commit()

    # Texto plano de unos KB: se indexa en el acto (sin extracción pesada, y el
    # troceado va en un hilo) para poder devolver el documento ya indexado.
    from app.services.kb_indexer import index_document_now

    await index_document_now(doc_id)
    await db.refresh(doc)
    return doc


@router.post("/notes")
async def create_note(
    body: NoteIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> DocumentOut:
    """Añade texto a la KB sin necesidad de archivo: crea un documento .txt
    indexado, por el mismo camino que la subida manual y que los aprendizajes."""
    doc = await create_text_document(db, body.titulo, body.contenido, user.id)
    await _audit_kb(db, user.id, "kb.note.created", doc.id, {"nombre": doc.nombre})

    return DocumentOut(
        id=doc.id,
        nombre=doc.nombre,
        formato=doc.formato.value,
        tamano_bytes=doc.tamano_bytes,
        status=doc.status.value,
        error_msg=doc.error_msg,
        num_chunks=doc.num_chunks,
        uploaded_at=doc.uploaded_at.isoformat(),
    )


@router.delete("/documents/{document_id}", response_model=OkResponse)
async def delete_document(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> OkResponse:
    doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Documento no encontrado")
    # Datos del documento ANTES de borrarlo: después de `db.delete` la instancia
    # ya no sirve para dejar rastro.
    nombre, formato = doc.nombre, doc.formato.value
    if doc.storage_path:
        # Solo borra si el path resuelto cae dentro del directorio de KB.
        try:
            resolved = Path(doc.storage_path).resolve()
            if str(resolved).startswith(str(KB_STORAGE_ROOT)) and resolved.is_file():
                resolved.unlink()
        except (FileNotFoundError, OSError):
            pass
    await db.delete(doc)  # CASCADE borra chunks
    await db.commit()
    # Borrar un documento es IRREVERSIBLE (se lleva por delante los chunks y el
    # fichero del disco) y hasta ahora no dejaba ni una línea: se sabía que la
    # KB había menguado, no quién. Va después del commit, como el resto de
    # borrados del panel, y es best-effort: no puede tumbar el borrado.
    await _audit_kb(
        db, user.id, "kb.document.deleted", document_id, {"nombre": nombre, "formato": formato}
    )
    return OkResponse()


# ── Reindexado ──────────────────────────────────────────────────────────────
#
# No existía: un documento solo se reindexaba al EDITAR su texto, así que quien
# subía PDFs antes de poner la clave de OpenAI se quedaba con ellos sin búsqueda
# semántica para siempre y la única salida era borrarlos y volver a subirlos uno
# a uno.


class ReindexOut(BaseModel):
    reindexados: int
    detalle: str


@router.post("/documents/{document_id}/reindex", response_model=ReindexOut)
async def reindex_document(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
) -> ReindexOut:
    """Vuelve a trocear e indexar un documento a partir de su archivo."""
    doc = (
        await db.execute(select(Document).where(Document.id == document_id))
    ).scalar_one_or_none()
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Documento no encontrado")

    from app.services.kb_indexer import schedule_indexing

    await schedule_indexing(doc.id)
    return ReindexOut(reindexados=1, detalle=f"«{doc.nombre}» en cola de reindexado.")


class ReindexAllIn(BaseModel):
    # Por defecto solo lo que está roto o a medias (lo que se quiere tras poner
    # la clave). `todos=True` fuerza la KB entera, que cuesta dinero en tokens.
    todos: bool = False


@router.post("/reindex", response_model=ReindexOut)
async def reindex_documents(
    body: ReindexAllIn | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
) -> ReindexOut:
    """Reindexa en bloque. Por defecto, solo los que lo necesitan."""
    todos = bool(body and body.todos)
    stmt = select(Document)
    if not todos:
        stmt = stmt.where(
            Document.status.in_(
                [DocumentStatus.error, DocumentStatus.indexado_sin_semantica]
            )
        )
    docs = (await db.execute(stmt)).scalars().all()

    from app.services.kb_indexer import schedule_indexing

    for d in docs:
        await schedule_indexing(d.id)
    if not docs:
        return ReindexOut(reindexados=0, detalle="No hay documentos que reindexar.")
    return ReindexOut(
        reindexados=len(docs),
        detalle=f"{len(docs)} documento(s) en cola de reindexado.",
    )


class KBStatusOut(BaseModel):
    """Salud de la KB para pintarla en la pantalla de Base de conocimiento."""

    embeddings_configurados: bool
    total: int
    indexados: int
    sin_semantica: int
    procesando: int
    con_error: int


@router.get("/status", response_model=KBStatusOut)
async def kb_status(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
) -> KBStatusOut:
    from sqlalchemy import func

    from app.providers.embeddings import embeddings_configured

    rows = (
        await db.execute(select(Document.status, func.count()).group_by(Document.status))
    ).all()
    counts = {s: int(n) for s, n in rows}
    return KBStatusOut(
        embeddings_configurados=await embeddings_configured(),
        total=sum(counts.values()),
        indexados=counts.get(DocumentStatus.indexado, 0),
        sin_semantica=counts.get(DocumentStatus.indexado_sin_semantica, 0),
        procesando=counts.get(DocumentStatus.procesando, 0),
        con_error=counts.get(DocumentStatus.error, 0),
    )


class ChunkOut(BaseModel):
    id: uuid.UUID
    contenido: str
    metadata: dict | None = None


@router.get("/documents/{document_id}/chunks", response_model=list[ChunkOut])
async def list_document_chunks(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[ChunkOut]:
    doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Documento no encontrado")
    rows = (
        await db.execute(
            select(Chunk).where(Chunk.document_id == document_id).order_by(Chunk.id)
        )
    ).scalars().all()
    return [
        ChunkOut(id=c.id, contenido=c.contenido, metadata=c.extra or None)
        for c in rows
    ]


# ── Edición de documentos de texto (txt/md) + versionado ────────────────────


class DocumentContentOut(BaseModel):
    id: uuid.UUID
    nombre: str
    formato: str
    contenido: str


class DocumentContentIn(BaseModel):
    contenido: str = Field(..., min_length=1, max_length=MAX_TEXT_CHARS)


class DocumentVersionOut(BaseModel):
    id: uuid.UUID
    contenido: str
    created_at: str


async def _get_editable_document(db: AsyncSession, document_id: uuid.UUID) -> Document:
    doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Documento no encontrado")
    if doc.formato not in EDITABLE_FORMATS:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Solo los documentos de texto (txt/md) se editan desde el panel. "
            "Para este formato, sube la versión nueva del archivo.",
        )
    return doc


def _read_document_text(doc: Document) -> str:
    """Lee el archivo del documento validando que sigue dentro del storage."""
    if not doc.storage_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "El documento no tiene archivo asociado")
    resolved = Path(doc.storage_path).resolve()
    if not str(resolved).startswith(str(KB_STORAGE_ROOT)) or not resolved.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Archivo no encontrado en el almacenamiento")
    return resolved.read_text(encoding="utf-8", errors="ignore")


async def _audit_kb(
    db: AsyncSession, user_id: uuid.UUID, action: str, doc_id: uuid.UUID, after: dict | None = None
) -> None:
    """Auditoría best-effort de acciones sobre la KB (nunca rompe la operación)."""
    try:
        from app.services.audit import record_audit

        await record_audit(
            db, user_id=user_id, action=action, entity="document", entity_id=doc_id, after=after
        )
        await db.commit()
    except Exception:
        pass


async def _apply_document_content(
    db: AsyncSession, doc: Document, new_content: str, user_id: uuid.UUID
) -> None:
    """Núcleo compartido de editar y restaurar: guarda la versión anterior,
    reescribe el archivo y reindexa (los chunks antiguos se borran al reindexar)."""
    previous = _read_document_text(doc)
    if previous.strip():
        db.add(DocumentVersion(document_id=doc.id, contenido=previous, created_by=user_id))
        # Recorta el historial: conserva las MAX_VERSIONS_PER_DOC más recientes.
        await db.flush()
        old_ids = (
            await db.execute(
                select(DocumentVersion.id)
                .where(DocumentVersion.document_id == doc.id)
                .order_by(desc(DocumentVersion.created_at), desc(DocumentVersion.id))
                .offset(MAX_VERSIONS_PER_DOC)
            )
        ).scalars().all()
        for vid in old_ids:
            v = await db.get(DocumentVersion, vid)
            if v:
                await db.delete(v)

    raw = new_content.encode("utf-8")
    Path(doc.storage_path).resolve().write_bytes(raw)
    doc.tamano_bytes = len(raw)
    doc.status = DocumentStatus.procesando
    doc.error_msg = None
    await db.commit()

    # Igual que la nota: es texto editable, no un PDF de 25 MB.
    from app.services.kb_indexer import index_document_now

    await index_document_now(doc.id)
    await db.refresh(doc)


@router.get("/documents/{document_id}/content", response_model=DocumentContentOut)
async def get_document_content(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
) -> DocumentContentOut:
    doc = await _get_editable_document(db, document_id)
    return DocumentContentOut(
        id=doc.id, nombre=doc.nombre, formato=doc.formato.value, contenido=_read_document_text(doc)
    )


@router.put("/documents/{document_id}")
async def update_document_content(
    document_id: uuid.UUID,
    body: DocumentContentIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> DocumentOut:
    contenido = body.contenido.strip()
    if not contenido:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El contenido no puede estar vacío")
    doc = await _get_editable_document(db, document_id)
    await _apply_document_content(db, doc, contenido, user.id)
    await _audit_kb(db, user.id, "kb.document.edited", doc.id, {"nombre": doc.nombre})
    return DocumentOut(
        id=doc.id,
        nombre=doc.nombre,
        formato=doc.formato.value,
        tamano_bytes=doc.tamano_bytes,
        status=doc.status.value,
        error_msg=doc.error_msg,
        num_chunks=doc.num_chunks,
        uploaded_at=doc.uploaded_at.isoformat(),
    )


@router.get("/documents/{document_id}/versions", response_model=list[DocumentVersionOut])
async def list_document_versions(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[DocumentVersionOut]:
    doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Documento no encontrado")
    rows = (
        await db.execute(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == document_id)
            .order_by(desc(DocumentVersion.created_at), desc(DocumentVersion.id))
            .limit(MAX_VERSIONS_PER_DOC)
        )
    ).scalars().all()
    return [
        DocumentVersionOut(id=v.id, contenido=v.contenido, created_at=v.created_at.isoformat())
        for v in rows
    ]


@router.post("/documents/{document_id}/versions/{version_id}/restore")
async def restore_document_version(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> DocumentOut:
    """Restaura una versión anterior. El contenido actual se guarda como versión
    nueva antes de sobrescribir, así que restaurar también es reversible."""
    doc = await _get_editable_document(db, document_id)
    version = (
        await db.execute(
            select(DocumentVersion).where(
                DocumentVersion.id == version_id, DocumentVersion.document_id == document_id
            )
        )
    ).scalar_one_or_none()
    if not version:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Versión no encontrada")
    await _apply_document_content(db, doc, version.contenido, user.id)
    await _audit_kb(db, user.id, "kb.document.restored", doc.id, {"version_id": str(version_id)})
    return DocumentOut(
        id=doc.id,
        nombre=doc.nombre,
        formato=doc.formato.value,
        tamano_bytes=doc.tamano_bytes,
        status=doc.status.value,
        error_msg=doc.error_msg,
        num_chunks=doc.num_chunks,
        uploaded_at=doc.uploaded_at.isoformat(),
    )


@router.post("/search")
@limiter.limit(limit_spec("30/minute"))
async def search_kb(
    request: Request,
    req: KBSearchRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> list[KBSearchResult]:
    from sqlalchemy import text as sql_text

    # El cupo se consume ANTES de tocar embeddings, que es lo que cuesta. Mismo
    # patrón que la re-redacción de borradores (conversations.refine_draft).
    allowed, _used = await consume_user_daily_quota(
        "kb_search", user.id, KB_SEARCH_DAILY_LIMIT_PER_USER
    )
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Has llegado a tu tope diario de búsquedas en la base de conocimiento "
            f"({KB_SEARCH_DAILY_LIMIT_PER_USER}). Se reinicia mañana.",
        )

    embeddings = await embed_texts([req.query])
    if not embeddings or not any(embeddings[0]):
        return []
    embedding_str = "[" + ",".join(f"{x:.6f}" for x in embeddings[0]) + "]"
    rows = (
        await db.execute(
            sql_text(
                "SELECT id, document_id, contenido, metadata, similarity "
                "FROM match_chunks(CAST(:emb AS vector), :threshold, :limit)"
            ),
            {"emb": embedding_str, "threshold": 0.3, "limit": req.top_k},
        )
    ).fetchall()
    return [
        KBSearchResult(
            chunk_id=r.id,
            document_id=r.document_id,
            contenido=r.contenido,
            similarity=float(r.similarity),
            metadata=r.metadata,
        )
        for r in rows
    ]
