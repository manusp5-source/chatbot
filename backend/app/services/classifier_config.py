"""Lectura de la config del clasificador (singleton). Devuelve un snapshot
inmutable para evitar objetos ORM detached fuera de la sesión."""
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from app.db.session import db_session
from app.models.classifier_config import SINGLETON_ID, ClassifierConfig


@dataclass
class ClassifierRuntime:
    enabled: bool
    channels: list[str]
    instructions: str
    model_name: str
    temperature: float
    llm_provider_id: "uuid.UUID | None" = None


async def get_classifier_runtime() -> ClassifierRuntime | None:
    async with db_session() as db:
        cfg = (
            await db.execute(select(ClassifierConfig).where(ClassifierConfig.id == SINGLETON_ID))
        ).scalar_one_or_none()
        if not cfg:
            return None
        return ClassifierRuntime(
            enabled=bool(cfg.enabled),
            channels=list(cfg.channels or []),
            instructions=cfg.instructions,
            model_name=cfg.model_name,
            temperature=float(cfg.temperature),
            llm_provider_id=cfg.llm_provider_id,
        )
