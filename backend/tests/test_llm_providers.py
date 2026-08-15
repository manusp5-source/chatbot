"""Proveedores LLM configurables: cifrado en reposo, resolución por agente,
retrocompatibilidad del proveedor por defecto. DB-gated."""
import asyncio
import uuid

import pytest


def _db_available() -> bool:
    try:
        async def _check():
            from sqlalchemy import text
            from app.db.session import db_session
            async with db_session() as db:
                await db.execute(text("SELECT 1"))
        asyncio.run(_check())
        return True
    except Exception:
        return False


pytestmark_db = pytest.mark.skipif(
    not _db_available(),
    reason="DB no disponible en este entorno (se ejecuta en CI con Postgres)",
)


@pytestmark_db
@pytest.mark.asyncio
async def test_provider_key_encrypted_at_rest_and_resolves():
    from sqlalchemy import select, text

    from app.db.session import db_session
    from app.models.llm_provider import LLMProvider
    from app.providers.llm import resolve_llm_provider
    from app.providers.llm.openai_client import FallbackLLMProvider

    secret = f"sk-{uuid.uuid4().hex}"
    async with db_session() as db:
        p = LLMProvider(
            name=f"prov-{uuid.uuid4().hex[:8]}",
            base_url="https://api.deepseek.com",
            api_key=secret,
            accepts_temperature=True,
            is_default=False,
        )
        db.add(p)
        await db.flush()
        pid = p.id
        await db.commit()

    try:
        # En BD el valor está cifrado (BYTEA): el secreto no aparece en claro.
        async with db_session() as db:
            raw = (
                await db.execute(
                    text("SELECT api_key FROM llm_providers WHERE id = :i"), {"i": str(pid)}
                )
            ).scalar_one()
            assert secret.encode() not in bytes(raw)
            # El ORM lo descifra al leer.
            row = (
                await db.execute(select(LLMProvider).where(LLMProvider.id == pid))
            ).scalar_one()
            assert row.api_key == secret

        # resolve_llm_provider construye el primario con la config de la fila.
        prov = await resolve_llm_provider(pid)
        assert isinstance(prov, FallbackLLMProvider)
        assert prov.primary.explicit_api_key == secret
        assert prov.primary.explicit_base_url == "https://api.deepseek.com"
        assert prov.primary.accepts_temperature is True
    finally:
        async with db_session() as db:
            await db.execute(text("DELETE FROM llm_providers WHERE id = :i"), {"i": str(pid)})
            await db.commit()


@pytestmark_db
@pytest.mark.asyncio
async def test_default_provider_uses_openai_credential():
    # provider_id None → proveedor por defecto (OpenAI sembrado, base_url vacío)
    # → se comporta como el primario por credencial openai_api_key (retrocompat).
    from app.providers.llm import resolve_llm_provider
    from app.providers.llm.openai_client import FallbackLLMProvider

    prov = await resolve_llm_provider(None)
    assert isinstance(prov, FallbackLLMProvider)
    assert prov.primary.api_key_credential == "openai_api_key"

    # provider_id inexistente → también cae al default.
    prov2 = await resolve_llm_provider(uuid.uuid4())
    assert isinstance(prov2, FallbackLLMProvider)
