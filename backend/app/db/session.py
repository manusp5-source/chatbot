import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.core.config import settings


class Base(DeclarativeBase):
    pass


# Serializador JSON para columnas JSON/JSONB (p.ej. audit_log.before/after).
# El por defecto de SQLAlchemy es json.dumps a secas, que revienta con datetime,
# UUID o Decimal. Un TypeError en una escritura JSONB dentro de un endpoint se
# propaga como 500 SIN cabeceras CORS, y el navegador lo reporta como error de
# CORS ocultando la causa real. default=str los serializa de forma segura sin
# alterar el comportamiento de los valores ya JSON-nativos.
def _json_serializer(value: object) -> str:
    return json.dumps(value, default=str)


# Estrategia de pool por TIPO de proceso:
#
#   - Celery (worker/beat) → NullPool: cada task corre en un event loop nuevo
#     (asyncio.run por task). Un pool tradicional mantendria conexiones asyncpg
#     atadas al loop anterior (cerrado) y fallaria con "attached to a different
#     loop" / "Event loop is closed". Conexion nueva por uso = robustez total.
#   - API (uvicorn) → pool persistente: el proceso vive en UN solo event loop,
#     asi que el pool es seguro y evita pagar un handshake TCP+TLS+auth de
#     Postgres por CADA request (que era el coste de NullPool en el endpoint
#     mas caliente). pool_pre_ping descarta conexiones muertas y pool_recycle
#     las renueva cada 30 min (firewalls/idle timeouts).
#
# Heuristica: si el ejecutable del proceso es celery → NullPool. Override
# explicito con la env DB_POOL:
#   DB_POOL=null  → fuerza NullPool (p.ej. scripts one-shot con asyncio.run,
#                   o los tests, que crean un loop por test).
#   DB_POOL=queue → fuerza el pool persistente.
_pool_override = (os.getenv("DB_POOL") or "").strip().lower()
_is_celery_process = "celery" in os.path.basename(sys.argv[0]).lower() if sys.argv and sys.argv[0] else False
_use_null_pool = _pool_override == "null" or (_pool_override != "queue" and _is_celery_process)

# Timeouts de asyncpg. Sin ellos, conectar a una BD que no responde (o una
# consulta que se queda atascada) bloquea el await SIN LIMITE. En el worker,
# donde cada task abre conexion nueva (NullPool) y corre en un hilo, eso se
# traga el hilo en silencio: ni log, ni excepcion, ni reintento.
_CONNECT_ARGS = {"timeout": 10, "command_timeout": 120}

if _use_null_pool:
    engine = create_async_engine(
        settings.DATABASE_URL,
        echo=False,
        future=True,
        poolclass=NullPool,
        json_serializer=_json_serializer,
        connect_args=_CONNECT_ARGS,
    )
else:
    engine = create_async_engine(
        settings.DATABASE_URL,
        echo=False,
        future=True,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=1800,
        json_serializer=_json_serializer,
        connect_args=_CONNECT_ARGS,
    )

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def db_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
