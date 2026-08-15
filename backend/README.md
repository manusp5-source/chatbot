# Backend

API (FastAPI), agente de IA, tareas en segundo plano (Celery) y modelo de datos
(SQLAlchemy + Alembic) del chatbot. Python 3.12.

Las pruebas están en `tests/` y se lanzan con `pytest -q` **desde esta
carpeta**. El mapa de módulos, cómo levantar el entorno y cómo crear una
migración están en el [`CLAUDE.md`](../CLAUDE.md) de la raíz.
