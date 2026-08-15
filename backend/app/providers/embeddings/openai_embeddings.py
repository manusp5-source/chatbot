"""Embeddings de OpenAI para la base de conocimiento.

Sin clave de OpenAI seguimos devolviendo vectores de ceros a propósito: la
búsqueda de la KB degrada sola a texto completo y el sistema sigue funcionando.
Lo que NO puede pasar es que eso sea invisible — un documento indexado con ceros
queda "en verde" en el panel y su búsqueda semántica no funciona.

Por eso hay dos entradas:

  - `embed_texts(texts)`: como siempre (ceros si no hay clave). La usan las
    rutas de búsqueda, que ya saben degradar.
  - `embed_texts_with_status(texts)`: devuelve además si los vectores son
    REALES. La usa el indexador para marcar el documento como "indexado sin
    búsqueda semántica" en vez de darlo por bueno.
"""
from dataclasses import dataclass

from openai import AsyncOpenAI

from app.core.config import settings
from app.core.logging import get_logger
from app.providers.openai_factory import EMBEDDING_TIMEOUT_SECONDS, get_async_openai
from app.services.credentials import get_credential

logger = get_logger(__name__)

# Dimensión de text-embedding-3-small, que es la de la columna `vector` en BD.
EMBEDDING_DIM = 1536


@dataclass
class EmbeddingResult:
    """Vectores + si son reales.

    `ok=False` significa "esto son ceros de relleno": la fila se guarda para no
    romper el pipeline, pero no sirve para buscar por significado.
    """

    vectors: list[list[float]]
    ok: bool
    reason: str | None = None


async def _get_client() -> AsyncOpenAI | None:
    api_key = await get_credential("openai_api_key")
    if not api_key:
        return None
    return get_async_openai(api_key=api_key, timeout=EMBEDDING_TIMEOUT_SECONDS)


async def embeddings_configured() -> bool:
    """True si hay clave para generar embeddings de verdad."""
    return bool(await get_credential("openai_api_key"))


async def embed_texts_with_status(texts: list[str]) -> EmbeddingResult:
    if not texts:
        return EmbeddingResult(vectors=[], ok=True)
    client = await _get_client()
    if client is None:
        logger.warning("embeddings.no_key", count=len(texts))
        # Se avisa en "Logs en vivo": indexar con ceros y no decir nada dejaba al
        # usuario con la KB "en verde" y la búsqueda semántica muerta.
        try:
            from app.services.runtime_logs import push_runtime_log

            await push_runtime_log(
                level="error",
                event="embeddings.no_key",
                message=(
                    "No hay clave de OpenAI: la base de conocimiento se indexa SIN "
                    "búsqueda semántica (solo texto completo). Configura "
                    "openai_api_key en Admin → Credenciales y reindexa."
                ),
            )
        except Exception:  # noqa: BLE001 — el aviso nunca rompe la indexación
            pass
        return EmbeddingResult(
            vectors=[[0.0] * EMBEDDING_DIM for _ in texts],
            ok=False,
            reason="Falta la clave de OpenAI: sin búsqueda semántica.",
        )
    resp = await client.embeddings.create(
        model=settings.OPENAI_EMBEDDING_MODEL,
        input=texts,
    )
    # Telemetry best-effort: persiste tokens del embedding (sin bloquear).
    try:
        from app.services.usage_tracker import track_usage
        if resp.usage:
            await track_usage(
                source="rag",
                model=settings.OPENAI_EMBEDDING_MODEL,
                prompt_tokens=resp.usage.prompt_tokens or 0,
                completion_tokens=0,
            )
    except Exception:
        pass
    return EmbeddingResult(vectors=[d.embedding for d in resp.data], ok=True)


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Solo los vectores (compatibilidad). Ceros si no hay clave."""
    return (await embed_texts_with_status(texts)).vectors
