import asyncio
import uuid

from celery import shared_task

from app.core.logging import get_logger

logger = get_logger(__name__)


@shared_task(name="app.tasks.index_document.index_document", bind=True, max_retries=2)
def index_document(self, document_id: str) -> None:
    from app.services.kb_indexer import index_document_by_id

    try:
        asyncio.run(index_document_by_id(uuid.UUID(document_id)))
    except Exception as exc:
        logger.error("index_document.error", document_id=document_id, error=str(exc))
        raise self.retry(exc=exc, countdown=20)
