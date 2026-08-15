# Despliegue

La guía de despliegue es **[`DEPLOY_EASYPANEL.md`](../DEPLOY_EASYPANEL.md)**, en la raíz del
repositorio. No hay ninguna otra.

Esta carpeta no contiene ficheros de despliegue. Están en su sitio:

| Qué | Dónde |
|---|---|
| Compose de producción | `docker-compose.easypanel.yml` (raíz) |
| Compose de desarrollo local | `docker-compose.yml` (raíz) |
| Variables de producción | `.env.example` (raíz) |
| Imágenes | `backend/Dockerfile`, `frontend/Dockerfile`, `mcp-server/Dockerfile` |
| nginx del panel | `frontend/nginx.conf` |
| Migraciones y seed | los ejecuta solo `backend/entrypoint.sh` al arrancar |

> **No despliegues con `docker-compose.yml` ni con `.env.desarrollo.example`.** Son los de
> desarrollo local: traen `APP_ENV=development`, que desactiva las comprobaciones
> de arranque y deja pasar el usuario administrador de ejemplo que está escrito
> en este mismo repositorio. Producción va con `docker-compose.easypanel.yml` y
> `.env.example`.
