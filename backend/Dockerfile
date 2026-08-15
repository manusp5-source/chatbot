# cache-bust 2026-05-17 18:15:39 UTC: force rebuild after switching build type from Nixpacks to Dockerfile
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# pg_dump/pg_restore 17 desde el repo oficial PGDG, para los backups de
# Postgres (tarea Celery backup_db). La BD de PRODUCCIÓN (EasyPanel,
# el servicio de base de datos) corre pgvector/pgvector:pg17 y pg_dump ABORTA si su
# versión es MENOR que la del server ("aborting because of server version
# mismatch") — con postgresql-client-16 el backup de las 05:00 fallaba.
# pg_dump 17 también vuelca servers 16 (compose local pg16), así que fijar
# el 17 vale para ambos. Si la BD sube a pg18, subir este paquete a la vez.
# La clave del repo va por signed-by (sin apt-key).
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] http://apt.postgresql.org/pub/repos/apt $(. /etc/os-release; echo "$VERSION_CODENAME")-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-17 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN chmod +x entrypoint.sh

# Usuario sin privilegios + dirs de datos preparados. El entrypoint corre
# inicialmente como root para poder ajustar el ownership de volúmenes
# montados (EasyPanel los puede inicializar con uid 0) y después baja a
# 'app' vía gosu antes de ejecutar uvicorn / celery.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data/audios /data/kb_docs \
    && chown -R app:app /app /data

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
