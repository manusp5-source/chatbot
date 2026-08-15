"""Helper centralizado para registrar entries en audit_log.

Uso:
    await record_audit(db, user.id, "credential.updated", "credential", cred.id,
                       before={"value": "***"}, after={"value": "***"})

NOTA: por simplicidad Fase 1 no usamos middleware automático global. Los
endpoints sensibles llaman explícitamente a esta función. Es opt-in pero
claro: cada audit log se ve y se entiende dónde se genera.
"""
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog


async def record_audit(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    action: str,
    entity: str,
    entity_id: uuid.UUID | None = None,
    before: dict | None = None,
    after: dict | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user_id,
            action=action,
            entity=entity,
            entity_id=entity_id,
            before=before,
            after=after,
            ip=ip,
            user_agent=user_agent,
        )
    )
