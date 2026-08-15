from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware

from app.core.security_headers import SecurityHeadersMiddleware
from fastapi.responses import FileResponse, JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db

from app.api import admin as admin_router
from app.api import agent_api as agent_api_router
from app.api import auth as auth_router
from app.api import backups as backups_router
from app.api import contacts as contacts_router
from app.api import conversations as conversations_router
from app.api import health as health_router
from app.api import knowledge_base as kb_router
from app.api import learning as learning_router
from app.api import oauth as oauth_router
from app.api import push as push_router
from app.api import tags as tags_router
from app.api import uploads as uploads_router
from app.api import voice as voice_router
from app.api import webchat as webchat_router
from app.api import webhooks as webhooks_router
from app.api.deps import get_current_user
from app.api.uploads import attachment_belongs_to_conversation
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.core.ratelimit import (
    DEFAULT_API_RATE_LIMIT,
    check_proxy_config,
    limit_spec,
    make_limiter,
)
from app.models.user import User

configure_logging("INFO" if settings.is_production else "DEBUG")
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("app.start", env=settings.APP_ENV, base_url=settings.APP_BASE_URL)
    # Rate-limit: la cadena de proxies real no se puede medir sin tráfico, pero
    # sí se puede avisar de la configuración que la rompe (ver check_proxy_config).
    # Un TRUSTED_PROXY_COUNT corto mete todo el tráfico en el mismo cubo y cinco
    # intentos fallidos tumban el login de TODOS.
    try:
        for w in check_proxy_config():
            logger.error("app.start.ratelimit_config", warning=w)
            try:
                from app.services.runtime_logs import push_runtime_log
                await push_runtime_log(level="error", event="ratelimit.config", message=w)
            except Exception:
                pass
    except Exception as e:
        logger.warning("app.start.proxy_check_failed", error=str(e))
    # Defaults de fábrica todavía puestos. `validate_production` ya aborta el
    # arranque si esto pasa en producción, pero solo mira APP_ENV: la forma fácil
    # de acabar con una instalación abierta a internet y la contraseña de ejemplo
    # es seguir el camino de desarrollo y desplegarlo tal cual, y ahí no saltaba
    # absolutamente nada. Esto no bloquea: avisa donde se ve.
    try:
        from app.core.config import avisos_de_configuracion_insegura

        for aviso in avisos_de_configuracion_insegura(settings):
            logger.error("app.start.config_insegura", warning=aviso)
            try:
                from app.services.runtime_logs import push_runtime_log

                await push_runtime_log(
                    level="error", event="config.insegura", message=aviso
                )
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001 — un aviso nunca tumba el arranque
        logger.warning("app.start.config_check_failed", error=str(e))
    # Calienta la caché de precios (llm_model_price) para que estimate_cost_usd
    # use los precios actualizados en las rutas síncronas (logging, budget).
    try:
        from app.services.llm_pricing import refresh_price_cache
        await refresh_price_cache()
    except Exception as e:  # nunca bloquea el arranque por el telemetry de coste
        logger.warning("app.start.price_cache_warm_failed", error=str(e))
    yield
    # Cierra los pools de conexiones de los clientes OpenAI cacheados (los de
    # este loop). Sin esto, los sockets quedan colgando al apagar.
    try:
        from app.providers.openai_factory import close_openai_clients
        await close_openai_clients()
    except Exception as e:
        logger.warning("app.stop.openai_close_failed", error=str(e))
    # Pools de Redis del proceso API: el del bus de eventos (publisher) y el
    # general. Se cachean por event loop, así que al apagar hay que soltarlos
    # explícitamente o los sockets quedan colgando.
    try:
        from app.core.events import close_event_bus
        await close_event_bus()
    except Exception as e:
        logger.warning("app.stop.event_bus_close_failed", error=str(e))
    try:
        from app.core.redis import close_redis
        await close_redis()
    except Exception as e:
        logger.warning("app.stop.redis_close_failed", error=str(e))
    logger.info("app.stop")


# En producción se ocultan /docs, /redoc y /openapi.json para no enumerar la API.
app = FastAPI(
    title=f"{settings.APP_NAME} API",
    version="0.1.0",
    description="API del chatbot de atención al cliente",
    lifespan=lifespan,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)

# Rate limiter compartido (lo importan los routers que lo necesiten). Usa la IP
# del cliente derivada de X-Forwarded-For cuando estamos detrás de proxy.
#
# `default_limits` es el TECHO por defecto de toda la API: hasta ahora solo
# tenían límite el login, el widget y los webhooks, así que cualquier otra ruta
# (incluidas las que llaman al modelo o exportan la base entera) se podía llamar
# en bucle sin freno. Se aplica vía SlowAPIMiddleware a todas las rutas y se
# SUMA a los límites específicos del decorador (gana el más restrictivo).
limiter = make_limiter(default_limits=[DEFAULT_API_RATE_LIMIT])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
)

# Cabeceras de seguridad para todas las respuestas HTTP del backend.
app.add_middleware(SecurityHeadersMiddleware)


# F4: CORS abierto para el widget web. El widget vive en webs de los
# clientes (dominios desconocidos a priori), así que `/widget.js` y los
# endpoints `/api/v1/webchat/*` aceptan peticiones de cualquier origen.
# No usamos allow_credentials → el widget no manda cookies, solo body +
# Content-Type, y el session_token va dentro del body, no en cookies.
@app.middleware("http")
async def webchat_cors_middleware(request: Request, call_next):
    path = request.url.path
    is_webchat = path.startswith("/api/v1/webchat") or path == "/widget.js"
    if is_webchat and request.method == "OPTIONS":
        return JSONResponse(
            content=None,
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
                "Access-Control-Max-Age": "86400",
            },
        )
    response = await call_next(request)
    if is_webchat:
        response.headers["Access-Control-Allow-Origin"] = "*"
        # Sobreescribe el CORP "same-site" del middleware global para que
        # el widget pueda ser embebido y llamado desde dominios externos.
        response.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("unhandled", path=str(request.url.path), error=str(exc))
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "message": "Error interno del servidor"}},
    )


# Health en raíz (para healthcheck Docker), resto bajo /api/v1
app.include_router(health_router.router)

api_v1 = APIRouter(prefix="/api/v1")
api_v1.include_router(auth_router.router)
api_v1.include_router(webhooks_router.router)
api_v1.include_router(contacts_router.router)
api_v1.include_router(tags_router.router)
api_v1.include_router(conversations_router.router)
api_v1.include_router(kb_router.router)
api_v1.include_router(learning_router.router)
api_v1.include_router(oauth_router.router)
api_v1.include_router(admin_router.router)
# Copias de seguridad (sección propia del panel admin)
api_v1.include_router(backups_router.router)
# API para agentes externos del operador (tokens con ámbitos, ver agent_api.py)
api_v1.include_router(agent_api_router.router)
api_v1.include_router(uploads_router.router)
api_v1.include_router(voice_router.router)
api_v1.include_router(webchat_router.router)
api_v1.include_router(push_router.router)
app.include_router(api_v1)

# WebSocket en /api/v1/ws/inbox y /api/v1/webchat/ws/{conv_id}
app.include_router(conversations_router.ws_router, prefix="/api/v1")
app.include_router(webchat_router.ws_router, prefix="/api/v1")


# Servir audios localmente — autenticado, con resolución segura del path para
# evitar traversal (..). Solo usuarios autenticados pueden descargarlos.
_AUDIO_ROOT = Path(settings.AUDIO_STORAGE_PATH).resolve()


@app.get("/audios/{filename}", include_in_schema=False)
@limiter.limit(limit_spec("240/minute"))
async def serve_audio(
    request: Request,
    filename: str,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    candidate = (_AUDIO_ROOT / filename).resolve()
    if _AUDIO_ROOT not in candidate.parents and candidate != _AUDIO_ROOT:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ruta inválida")
    # El fichero tiene que pertenecer a una conversación. Sin esto, cualquier
    # sesión válida se descarga CUALQUIER audio del volumen sabiendo el nombre,
    # incluido lo que ya no cuelga de ninguna conversación (mismo criterio que
    # /uploads/{filename}, ver app/api/uploads.py).
    if not await attachment_belongs_to_conversation(db, "/audios/", filename):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Audio no encontrado")
    if not candidate.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Audio no encontrado")
    return FileResponse(candidate)


# F4: snippet del widget de chat embebible. Servido público (sin auth) en
# la raíz del backend para que la web del cliente lo pueda incluir con
# `<script src="...">`. Cache-friendly: el archivo cambia poco.
_STATIC_ROOT = Path(__file__).parent / "static"


@app.get("/widget.js", include_in_schema=False)
async def serve_widget() -> FileResponse:
    path = _STATIC_ROOT / "widget.js"
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "widget.js no encontrado")
    # CORP cross-origin: el widget se carga desde dominios externos (la web
    # del cliente). Sin este header, los browsers modernos lo bloquean con
    # ERR_BLOCKED_BY_RESPONSE.NotSameSite. El middleware global pone
    # "same-site" por defecto; aquí lo sobreescribimos.
    return FileResponse(
        path,
        media_type="application/javascript",
        headers={
            "Cache-Control": "public, max-age=300",
            "Cross-Origin-Resource-Policy": "cross-origin",
            "Access-Control-Allow-Origin": "*",
        },
    )
