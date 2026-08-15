#!/usr/bin/env bash
set -euo pipefail

# Si arrancamos como root (típico la primera vez que un volumen externo se
# monta o cuando viene de un contenedor anterior que corría como root),
# normalizamos el ownership y re-ejecutamos este mismo script como 'app'.
if [ "$(id -u)" -eq 0 ]; then
  chown -R app:app /data 2>/dev/null || true
  exec gosu app "$0" "$@"
fi

# A partir de aquí corremos como user 'app' (uid 1000).

echo "[entrypoint] esperando a Postgres..."
for i in $(seq 1 60); do
  if python -c "
import os, asyncio, asyncpg, re
url = os.environ['DATABASE_URL'].replace('+asyncpg', '').replace('postgresql+psycopg2', 'postgresql')
async def chk():
    c = await asyncpg.connect(url)
    await c.close()
asyncio.run(chk())
" 2>/dev/null; then
    echo "[entrypoint] Postgres listo"
    break
  fi
  sleep 1
done

# ¿Hay ya algún usuario administrador activo en la base de datos?
# Sirve para decidir qué hacer si el seed falla: sin admin, el panel se queda
# con un login que no acepta a NADIE, y eso no puede pasar en silencio.
hay_admin() {
  python - <<'PY' 2>/dev/null
import asyncio
import os
import sys

import asyncpg

url = (
    os.environ["DATABASE_URL"]
    .replace("+asyncpg", "")
    .replace("postgresql+psycopg2", "postgresql")
)


async def contar():
    c = await asyncpg.connect(url)
    try:
        return await c.fetchval(
            "SELECT count(*) FROM users WHERE role = 'admin' AND activo"
        )
    finally:
        await c.close()


sys.exit(0 if (asyncio.run(contar()) or 0) > 0 else 1)
PY
}

if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
  echo "[entrypoint] aplicando migraciones..."
  alembic upgrade head

  echo "[entrypoint] sembrando datos iniciales..."
  # El seed crea el usuario admin, el prompt del agente y las etiquetas base.
  # ANTES esto era `python -m app.scripts.seed || echo "seed falló"`: el
  # contenedor llegaba a "sano" sin usuario admin y quien entraba se encontraba
  # un login que no aceptaba a nadie, sin ningún error visible por ninguna
  # parte. Ahora falla RUIDOSAMENTE y, si no hay admin, para el arranque.
  if ! python -m app.scripts.seed; then
    echo "[entrypoint] el seed ha fallado; reintentando una vez en 5 s..."
    sleep 5
    if ! python -m app.scripts.seed; then
      echo ""
      echo "==================================================================="
      echo "[entrypoint] ERROR: NO SE HAN PODIDO SEMBRAR LOS DATOS INICIALES"
      echo "==================================================================="
      echo "El error concreto está en las líneas de arriba de este mismo log."
      echo ""
      echo "Causas habituales, en orden de probabilidad:"
      echo "  1. INITIAL_ADMIN_EMAIL o INITIAL_ADMIN_PASSWORD mal puestos"
      echo "     (vacíos, con espacios de sobra o con el valor de ejemplo)."
      echo "  2. La base de datos no admite escrituras: disco lleno del VPS o"
      echo "     permisos del usuario de Postgres."
      echo "  3. Una migración a medias: mira si arriba hay errores de alembic."
      echo ""
      echo "Cómo arreglarlo:"
      echo "  - Revisa las variables del servicio 'app' en EasyPanel y despliega"
      echo "    otra vez. El seed es idempotente: repetirlo no duplica nada."
      echo "  - Para lanzarlo a mano desde la consola del contenedor:"
      echo "        python -m app.scripts.seed"
      echo ""

      if hay_admin; then
        echo "[entrypoint] AVISO: ya existe un usuario administrador en la base"
        echo "[entrypoint] de datos, así que el panel SÍ deja entrar. Arrancamos"
        echo "[entrypoint] igualmente, pero el resto del seed (prompt del agente,"
        echo "[entrypoint] etiquetas, agente de voz) puede estar incompleto."
        echo "[entrypoint] Arréglalo cuando puedas: no lo dejes así."
      else
        echo "[entrypoint] NO hay ningún usuario administrador en la base de"
        echo "[entrypoint] datos. Arrancar ahora dejaría el panel con un login"
        echo "[entrypoint] que no acepta a nadie y sin ningún aviso. Se aborta"
        echo "[entrypoint] el arranque a propósito: es preferible ver el fallo"
        echo "[entrypoint] aquí que descubrirlo intentando entrar."
        exit 1
      fi
    fi
  fi
fi

echo "[entrypoint] arrancando: $*"
exec "$@"
