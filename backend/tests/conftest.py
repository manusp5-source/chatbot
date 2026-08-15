"""Configuración global de tests."""
import os
import tempfile

os.environ.setdefault("APP_ENV", "test")
# Los tests DB-gated abren un event loop por test (asyncio.run / asyncio_mode
# auto). Con el pool persistente del proceso API, las conexiones quedarían
# atadas al loop del test anterior → NullPool explícito (igual que Celery).
os.environ.setdefault("DB_POOL", "null")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:test@localhost:5432/test")
os.environ.setdefault("JWT_SECRET", "test-secret-change-in-prod")
os.environ.setdefault("ENCRYPTION_KEY", "PqArc6c-wI8DK-bGwCPBlo4e_gg-Ym5mSnjh05FXQd4=")
# Rutas de almacenamiento. En local las define backend/.env, pero .env está en
# .gitignore y en la CI no existe: sin esto se usan los defaults de config.py
# (/data/audios), que en el runner no son escribibles. api/knowledge_base.py
# hace os.makedirs A NIVEL DE MÓDULO, así que el import revienta y se caen 39
# tests con PermissionError: '/data'. Un temporal vale para los tres entornos
# (CI, local, Docker) y además aísla: los tests no escriben en el storage real.
_TEST_STORAGE = os.path.join(tempfile.gettempdir(), "chatbot-tests-storage")
os.environ.setdefault("AUDIO_STORAGE_PATH", os.path.join(_TEST_STORAGE, "audios"))
os.environ.setdefault("UPLOADS_PATH", os.path.join(_TEST_STORAGE, "audios", "uploads"))
os.environ.setdefault("BACKUP_DIR", os.path.join(_TEST_STORAGE, "audios", "backups"))
