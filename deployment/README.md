# Deployment

The deployment guide is [`DEPLOY_EASYPANEL.md`](../DEPLOY_EASYPANEL.md) in the repository root. It is the only deployment guide.

This directory contains no deployment files. They live here:

| Item | Location |
|---|---|
| Production Compose file | `docker-compose.easypanel.yml` (root) |
| Local development Compose file | `docker-compose.yml` (root) |
| Production variables | `.env.example` (root) |
| Images | `backend/Dockerfile`, `frontend/Dockerfile`, `mcp-server/Dockerfile` |
| Panel nginx | `frontend/nginx.conf` |
| Migrations and seed data | Run only by `backend/entrypoint.sh` at startup |

> **Do not deploy with `docker-compose.yml` or `.env.desarrollo.example`.** They are for local development: they set `APP_ENV=development`, which disables startup checks and allows the example administrator credentials stored in this repository. Production uses `docker-compose.easypanel.yml` and `.env.example`.
