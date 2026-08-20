"""Servicio de indexación de la base de conocimiento.

Pipeline:
1. Extrae texto del documento según formato (PDF, DOCX, TXT, MD, CSV, XLSX)
2. Chunkea
3. Genera embeddings en batch
4. Persiste chunks con embedding

DOS COSAS IMPORTANTES SOBRE CÓMO SE EJECUTA:

a) La SUBIDA DE ARCHIVOS no indexa dentro del request: `schedule_indexing()` lo
   pone en segundo plano y devuelve al momento. Antes se hacía en línea, y la
   extracción de un PDF/Word/Excel (hasta 25 MB) es síncrona y pesada: mientras
   duraba se quedaba parado el event loop entero — no respondía el panel NI
   entraban los webhooks de WhatsApp e Instagram. El estado queda visible en la
   ficha del documento (`procesando` → `indexado` / `indexado_sin_semantica` /
   `error`) y hay reintentos automáticos.

   El camino de TEXTO (notas del panel, aprendizajes, edición de un .txt) usa
   `index_document_now()`, que sí espera: son unos KB, no hay extracción pesada
   y el llamante necesita el documento ya indexado.

b) La extracción va en un HILO (`anyio.to_thread`). pypdf/python-docx/pandas son
   síncronos y consumen CPU: aunque la tarea esté en segundo plano, correrlos en
   la corrutina volvería a bloquear el loop.

Por defecto la indexación corre EN EL PROCESO DE LA APP, no en Celery: en el
EasyPanel de producción el contenedor `worker` no ve el volumen donde `app`
acaba de escribir el archivo, y el worker además puede estar caído. Si en un
despliegue concreto el volumen sí se comparte, `KB_INDEX_VIA_CELERY=1` lo manda
a la cola.
"""
import asyncio
import csv
import os
import re
import uuid
from pathlib import Path

import anyio
import pandas as pd
import pypdf
from docx import Document as DocxDocument
from sqlalchemy import select

from app.core.logging import get_logger
from app.db.session import db_session
from app.models.chunk import Chunk
from app.models.document import Document, DocumentFormato, DocumentStatus
from app.providers.embeddings import embed_texts_with_status

logger = get_logger(__name__)

CHUNK_SIZE = 800       # caracteres aprox por chunk
CHUNK_OVERLAP = 100    # solape para no perder contexto en bordes

# Reintentos del indexado en segundo plano (fallos transitorios: rate limit de
# embeddings, hipo de red, BD ocupada). Espera creciente entre intentos.
MAX_INDEX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (5, 20)

# Aviso que se guarda en el documento cuando se ha indexado con vectores de
# ceros. Se ve tal cual en la pantalla de Base de conocimiento.
DEGRADED_MSG = (
    "Indexado SIN búsqueda semántica: falta la clave de OpenAI para generar "
    "embeddings. El agente solo encontrará este documento por coincidencia de "
    "palabras. Configura la clave en Admin → Credenciales y pulsa «Reindexar»."
)

# Referencias fuertes a las tareas en vuelo: sin esto el recolector de basura
# puede cargarse un asyncio.Task del que nadie guarda el resultado.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _use_celery() -> bool:
    return os.getenv("KB_INDEX_VIA_CELERY", "").strip().lower() in {"1", "true", "yes"}


async def schedule_indexing(document_id: uuid.UUID) -> str:
    """Pone el documento en cola de indexación y VUELVE INMEDIATAMENTE.

    Devuelve cómo se ha encolado ("celery" | "background") para poder trazarlo.
    El documento queda en `procesando`; el resultado se refleja en su ficha.
    """
    await _mark_processing(document_id)

    if _use_celery():
        try:
            from app.tasks.index_document import index_document

            index_document.delay(str(document_id))
            logger.info("kb.index.enqueued", document_id=str(document_id), via="celery")
            return "celery"
        except Exception as e:  # noqa: BLE001 — broker caído: seguimos en proceso
            logger.warning("kb.index.celery_enqueue_failed", error=str(e))

    task = asyncio.create_task(_run_with_retries(document_id))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    logger.info("kb.index.enqueued", document_id=str(document_id), via="background")
    return "background"


async def index_document_now(document_id: uuid.UUID) -> None:
    """Indexa AHORA y nunca lanza (el resultado queda en la ficha del documento).

    Para el camino de TEXTO (notas del panel, aprendizajes aprobados, edición de
    un .txt/.md): son unos KB, no hay extracción de PDF/Word/Excel que consuma
    CPU, y el troceado ya corre en un hilo — así que esperar aquí NO bloquea el
    event loop; la única espera real es la llamada de embeddings, que es
    asíncrona. A cambio, el endpoint puede devolver el documento ya indexado,
    que es lo que esperan quien crea la nota y el smoke test de aprendizajes.

    La subida de ARCHIVOS no usa esto: va por `schedule_indexing`.
    """
    try:
        await index_document_by_id(document_id)
    except Exception as e:  # noqa: BLE001 — el estado ya quedó en `error`
        logger.warning("kb.index.inline_failed", document_id=str(document_id), error=str(e))


async def _mark_processing(document_id: uuid.UUID) -> None:
    async with db_session() as db:
        doc = (
            await db.execute(select(Document).where(Document.id == document_id))
        ).scalar_one_or_none()
        if doc:
            doc.status = DocumentStatus.procesando
            doc.error_msg = None
            await db.commit()


async def _run_with_retries(document_id: uuid.UUID) -> None:
    """Ejecuta la indexación reintentando los fallos transitorios.

    Solo el ÚLTIMO intento fallido deja el documento en `error`: reintentar
    dejándolo en rojo por medio confundiría a quien mira el panel.
    """
    for attempt in range(1, MAX_INDEX_ATTEMPTS + 1):
        last = attempt == MAX_INDEX_ATTEMPTS
        try:
            await index_document_by_id(document_id, _mark_error=last)
            return
        except Exception as e:  # noqa: BLE001 — el detalle ya se registró dentro
            if last:
                logger.error(
                    "kb.index.gave_up",
                    document_id=str(document_id),
                    attempts=attempt,
                    error=str(e),
                )
                return
            delay = RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
            logger.warning(
                "kb.index.retry",
                document_id=str(document_id),
                attempt=attempt,
                retry_in_s=delay,
                error=str(e),
            )
            await asyncio.sleep(delay)


async def index_document_by_id(
    document_id: uuid.UUID, *, _mark_error: bool = True
) -> None:
    """Indexa un documento. Lanza si falla (tras dejarlo en `error`).

    `_mark_error=False` lo usa el reintentador para no pintar el documento en
    rojo entre intento e intento.
    """
    async with db_session() as db:
        doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
        if not doc:
            return
        path = Path(doc.storage_path) if doc.storage_path else None
        formato = doc.formato

    try:
        if not path or not path.exists():
            # Incluir el path en el error para diagnostico (cuando se ve en log y UI).
            stored = str(path) if path else "<sin path>"
            raise RuntimeError(f"Archivo no encontrado en storage (path guardado: {stored})")

        # EN UN HILO: pypdf / python-docx / pandas son síncronos y pesados.
        text_chunks = await anyio.to_thread.run_sync(_extract_chunks, path, formato)
        if not text_chunks:
            raise RuntimeError("No se extrajo contenido")

        # Embeddings en batch (OpenAI permite hasta 2048 inputs por llamada).
        # `ok=False` = vectores de ceros porque no hay clave: se guardan igual
        # (la búsqueda degrada a texto completo) pero el documento NO se da por
        # bueno, se marca como indexado sin semántica.
        result = await embed_texts_with_status([c["contenido"] for c in text_chunks])

        async with db_session() as db:
            # Reindexado (edición/restauración): fuera los chunks anteriores en la
            # misma transacción que inserta los nuevos, para no dejar duplicados
            # ni una ventana sin contenido si algo falla a medias.
            from sqlalchemy import delete as sa_delete

            await db.execute(sa_delete(Chunk).where(Chunk.document_id == document_id))
            for chunk_data, embedding in zip(text_chunks, result.vectors, strict=False):
                chunk = Chunk(
                    document_id=document_id,
                    contenido=chunk_data["contenido"],
                    embedding=embedding,
                    extra=chunk_data.get("metadata") or {},
                )
                db.add(chunk)
            doc = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one()
            doc.num_chunks = len(text_chunks)
            if result.ok:
                doc.status = DocumentStatus.indexado
                doc.error_msg = None
            else:
                doc.status = DocumentStatus.indexado_sin_semantica
                doc.error_msg = DEGRADED_MSG
            await db.commit()

        logger.info(
            "kb.indexed",
            document_id=str(document_id),
            chunks=len(text_chunks),
            semantic=result.ok,
        )
    except Exception as e:
        # El detalle completo (que puede incluir paths internos o stack trace)
        # va al log. Al usuario solo le devolvemos un mensaje genérico por
        # tipo de error para no filtrar rutas del servidor.
        logger.error("kb.index.error", document_id=str(document_id), error=str(e))
        if _mark_error:
            public_msg = _public_error_message(e)
            async with db_session() as db:
                doc = (
                    await db.execute(select(Document).where(Document.id == document_id))
                ).scalar_one_or_none()
                if doc:
                    doc.status = DocumentStatus.error
                    doc.error_msg = public_msg
                    await db.commit()
        raise


def _public_error_message(e: BaseException) -> str:
    """Mapa excepción → mensaje apto para mostrar al usuario.

    Para errores conocidos da un mensaje claro. Para el resto incluye el
    tipo de excepción y el mensaje original (recortado, sin paths internos)
    porque sin esa info debugar requiere ir al log del worker cada vez.
    """
    import re
    name = type(e).__name__
    mapping = {
        "FileNotFoundError": "Archivo no encontrado en el almacenamiento.",
        "PermissionError": "Sin permisos para leer el archivo.",
        "PdfReadError": "PDF inválido o corrupto.",
        "BadZipFile": "Archivo comprimido inválido (DOCX/XLSX corrupto).",
        "UnicodeDecodeError": "Codificación de texto no soportada.",
    }
    if name in mapping:
        return mapping[name]
    # Genérico con tipo + mensaje recortado. Quitamos rutas absolutas (/app/...)
    # para no filtrar estructura del filesystem.
    msg = str(e)
    msg = re.sub(r"/[\w./-]+", "<path>", msg)
    msg = msg[:200]
    return f"Error al procesar ({name}): {msg}" if msg else f"Error al procesar ({name})"


def _extract_chunks(path: Path, formato: DocumentFormato) -> list[dict]:
    if formato == DocumentFormato.txt or formato == DocumentFormato.md:
        text = path.read_text(encoding="utf-8", errors="ignore")
        return _chunk_text(text, source=path.name)
    if formato == DocumentFormato.pdf:
        return _extract_pdf(path)
    if formato == DocumentFormato.docx:
        return _extract_docx(path)
    if formato == DocumentFormato.csv:
        return _extract_csv(path)
    if formato == DocumentFormato.xlsx:
        return _extract_xlsx(path)
    return []


def _split_units(text: str) -> list[str]:
    """Divide el texto en unidades naturales: párrafos, y dentro de un párrafo
    demasiado largo, frases. Solo un párrafo sin puntuación mayor que
    CHUNK_SIZE se parte por posición (último recurso)."""
    units: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= CHUNK_SIZE:
            units.append(para)
            continue
        # Párrafo largo → frases (corte tras . ! ? seguidos de espacio).
        sentences = re.split(r"(?<=[.!?])\s+", para)
        for s in sentences:
            s = s.strip()
            if not s:
                continue
            if len(s) <= CHUNK_SIZE:
                units.append(s)
            else:
                # Frase interminable (listas sin puntos, texto pegado): corte duro.
                units.extend(
                    s[i : i + CHUNK_SIZE] for i in range(0, len(s), CHUNK_SIZE)
                )
    return units


def _titulo_del_documento(text: str) -> str:
    """El encabezado del documento, si lo tiene: la primera línea `# …`.

    Sirve para dar contexto a los chunks que no son el primero. Sin esto, el
    trozo que contiene "Blanqueamiento en clínica | 290 €" es una tabla suelta
    sin ninguna pista de a qué clínica ni a qué documento pertenece, mientras
    que el PRIMER chunk sí arrastra el título y por eso gana siempre en la
    búsqueda por significado — incluso preguntando por lo que está en el
    segundo. El síntoma es el peor posible para el negocio: el agente dice que
    no tiene un precio que la clínica sí tiene.

    Solo se mira el arranque del documento: un `#` a mitad de texto es una
    sección, no el título.
    """
    for linea in text.lstrip().splitlines()[:3]:
        linea = linea.strip()
        if linea.startswith("#"):
            return linea.lstrip("#").strip()
        if linea:
            break
    return ""


def _chunk_text(text: str, source: str, page: int | None = None) -> list[dict]:
    """Trocea respetando la ESTRUCTURA del texto: agrupa párrafos/frases
    completos hasta ~CHUNK_SIZE, con solape de la última unidad entre chunks.

    Antes se cortaba cada CHUNK_SIZE caracteres por posición fija, partiendo
    frases y Q&As por la mitad — el embedding de medio párrafo empareja peor
    y el chunk recuperado le llegaba al agente descontextualizado.

    Cada chunk que no es el primero se prefija con el título del documento, para
    que su embedding sepa de qué habla. Ver `_titulo_del_documento`.
    """
    text = text.strip()
    if not text:
        return []
    meta: dict = {"source": source}
    if page is not None:
        meta["page"] = page

    titulo = _titulo_del_documento(text)
    units = _split_units(text)
    out: list[dict] = []
    current: list[str] = []
    current_len = 0

    def _cerrar(partes: list[str]) -> None:
        """Cierra un chunk, anteponiéndole el título salvo en el primero.

        El primero ya lo lleva dentro (es el arranque del documento), así que
        repetirlo lo duplicaría.
        """
        cuerpo = "\n\n".join(partes)
        if titulo and out:
            cuerpo = f"{titulo}\n\n{cuerpo}"
        out.append({"contenido": cuerpo, "metadata": dict(meta)})

    for unit in units:
        # +2 por el separador "\n\n" al unir.
        if current and current_len + len(unit) + 2 > CHUNK_SIZE:
            _cerrar(current)
            # Solape semántico: arrastra la última unidad (acotada) al chunk
            # siguiente para no perder el hilo entre cortes.
            tail = current[-1][-CHUNK_OVERLAP:] if len(current[-1]) > CHUNK_OVERLAP else current[-1]
            current = [tail] if tail else []
            current_len = len(tail) + 2 if tail else 0
        current.append(unit)
        current_len += len(unit) + 2
    if current:
        _cerrar(current)
    return out


def _extract_pdf(path: Path) -> list[dict]:
    out: list[dict] = []
    with path.open("rb") as f:
        reader = pypdf.PdfReader(f)
        for i, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            out.extend(_chunk_text(text, source=path.name, page=i))
    return out


def _extract_docx(path: Path) -> list[dict]:
    doc = DocxDocument(str(path))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    text = "\n".join(paragraphs)
    return _chunk_text(text, source=path.name)


def _extract_csv(path: Path) -> list[dict]:
    out: list[dict] = []
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, start=1):
            contenido = " | ".join(f"{k}: {v}" for k, v in row.items() if v)
            if contenido:
                out.append({"contenido": contenido, "metadata": {"source": path.name, "row": i}})
    return out


def _extract_xlsx(path: Path) -> list[dict]:
    out: list[dict] = []
    sheets = pd.read_excel(path, sheet_name=None)
    for sheet_name, df in sheets.items():
        for i, row in df.iterrows():
            parts = [f"{col}: {val}" for col, val in row.items() if pd.notna(val) and str(val).strip()]
            if parts:
                out.append(
                    {
                        "contenido": " | ".join(parts),
                        "metadata": {"source": path.name, "sheet": sheet_name, "row": int(i) + 1},
                    }
                )
    return out
