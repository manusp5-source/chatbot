import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_token_from_request
from app.core.config import settings
from app.core.logging import get_logger
from app.core.ratelimit import client_ip_key, make_limiter
from app.core.redis import get_redis
from app.core.security import (
    LOGIN_FAIL_MAX,
    LoginCounterUnavailable,
    clear_login_failures,
    create_access_token,
    hash_password,
    login_failures,
    password_policy_error,
    register_login_failure,
    revoke_token,
    verify_password,
)
from app.db.session import get_db
from app.models.user import User
from app.providers.email.resend_client import send_email
from app.schemas.auth import LoginRequest, LoginResponse, UserOut
from app.schemas.common import OkResponse
from app.services.audit import record_audit
from app.services.google_login import build_login_url, fetch_identity

router = APIRouter(prefix="/auth", tags=["auth"])
limiter = make_limiter()
logger = get_logger(__name__)


def _client_ip(request: Request) -> str | None:
    return client_ip_key(request)


# El lockout por CUENTA (contador de fallos en Redis) vive en core/security.py
# — `login_failures` / `register_login_failure` / `clear_login_failures` — para
# que la política de "fallar cerrado si no se puede consultar el contador" esté
# en un solo sitio. Ver el comentario largo de ese módulo.


@router.post("/login", response_model=LoginResponse)
@limiter.limit("5/minute;30/hour")
async def login(
    request: Request,
    payload: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> LoginResponse:
    ip = _client_ip(request)
    ua = request.headers.get("user-agent")
    email = payload.email.lower()
    # Cuenta bloqueada temporalmente por intentos fallidos: cortamos ANTES de
    # verificar la contraseña (mismo mensaje exista o no la cuenta).
    try:
        fallos = await login_failures(email)
    except LoginCounterUnavailable as e:
        # Sin contador no hay bloqueo por fuerza bruta: se corta el login en vez
        # de dejar barra libre. El panel sin Redis no funciona igualmente.
        logger.error("auth.login.counter_unavailable", error=str(e))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "El servicio de autenticación no está disponible ahora mismo. "
            "Vuelve a intentarlo en un momento.",
        ) from e
    if fallos >= LOGIN_FAIL_MAX:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Demasiados intentos fallidos para esta cuenta. Espera 15 minutos e inténtalo de nuevo.",
        )
    user = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if not user or not user.activo or not verify_password(payload.password, user.password_hash):
        await register_login_failure(email)
        await record_audit(
            db,
            user_id=user.id if user else None,
            action="auth.login.failed",
            entity="user",
            entity_id=user.id if user else None,
            after={"email": email},
            ip=ip,
            user_agent=ua,
        )
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Credenciales inválidas")
    await clear_login_failures(email)
    token = create_access_token(subject=str(user.id), extra={"role": user.role.value})
    user.last_login = func.now()
    await record_audit(
        db,
        user_id=user.id,
        action="auth.login.success",
        entity="user",
        entity_id=user.id,
        ip=ip,
        user_agent=ua,
    )
    await db.commit()
    await db.refresh(user)
    return LoginResponse(access_token=token, user=UserOut.model_validate(user))


# ============================ Login con Google (OIDC) ============================
# "Iniciar sesión con Google" para usuarios YA existentes: emparejamos por email
# verificado. No crea usuarios — si el email no tiene cuenta activa en el panel,
# se rechaza. Reutiliza el mismo OAuth Client de Google que los servicios; solo
# cambia el scope (openid/email/profile) y la redirect URI del callback.

_GLOGIN_STATE_PREFIX = "authlogin:state:"
_GLOGIN_STATE_TTL = 600  # 10 min

# El `state` NO basta con guardarlo en Redis: Redis es global, así que el
# callback aceptaría cualquier state vivo lo hubiera pedido quien lo hubiera
# pedido. Un atacante arranca el flujo con SU cuenta y le pasa a la víctima el
# enlace del callback: la víctima termina con sesión abierta en la cuenta del
# atacante (session fixation). Por eso el state viaja ADEMÁS en esta cookie,
# que solo tiene el navegador que inició el flujo, y el callback exige que
# coincida. HttpOnly (invisible a JS), SameSite=Lax (el navegador sí la manda
# en la vuelta de Google, que es una navegación GET de primer nivel) y vida
# corta (la misma que el state en Redis).
_GLOGIN_STATE_COOKIE = "glogin_state"
# Acotada a la ruta del router: no viaja en el resto de peticiones de la API.
_GLOGIN_COOKIE_PATH = "/api/v1/auth"


def _login_redirect_back(*, token: str | None = None, error: str | None = None) -> RedirectResponse:
    """Vuelve a la página de login del frontend.

    El token va en el FRAGMENTO (#), no en la query: así no aparece en logs de
    servidor, historial ni cabecera Referer. El frontend lo lee y limpia el hash.
    """
    base = settings.FRONTEND_BASE_URL.rstrip("/")
    frag = f"#token={token}" if token else f"#google_error={error or 'unknown'}"
    return RedirectResponse(f"{base}/login{frag}", status_code=status.HTTP_302_FOUND)


def _finish_google_login(
    *, token: str | None = None, error: str | None = None
) -> RedirectResponse:
    """Cierra el flujo: vuelve al login y BORRA la cookie de state.

    Se usa en TODAS las salidas del callback (éxito y error) para que no quede
    un state reutilizable en el navegador.
    """
    response = _login_redirect_back(token=token, error=error)
    response.delete_cookie(_GLOGIN_STATE_COOKIE, path=_GLOGIN_COOKIE_PATH)
    return response


@router.get("/google/login")
@limiter.limit("15/minute")
async def google_login_start(request: Request) -> RedirectResponse:
    """Arranca el login con Google: redirige al consentimiento de Google."""
    state = secrets.token_urlsafe(32)
    await get_redis().set(_GLOGIN_STATE_PREFIX + state, "1", ex=_GLOGIN_STATE_TTL)
    try:
        url = await build_login_url(state)
    except RuntimeError as e:
        logger.warning("auth.google_login.not_configured", error=str(e))
        return _login_redirect_back(error="google_not_configured")
    response = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        _GLOGIN_STATE_COOKIE,
        state,
        max_age=_GLOGIN_STATE_TTL,
        httponly=True,
        # En desarrollo la API va por http://localhost: con Secure el navegador
        # no la mandaría y el login con Google dejaría de funcionar en local.
        secure=settings.is_production,
        samesite="lax",
        path=_GLOGIN_COOKIE_PATH,
    )
    return response


@router.get("/google/callback", include_in_schema=False)
async def google_login_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Callback de Google: valida el state, obtiene el email verificado y, si
    coincide con un usuario activo, emite nuestro JWT de siempre."""
    if error:
        return _finish_google_login(error="google_denied")
    if not code or not state:
        return _finish_google_login(error="google_bad_request")
    # 1) ¿Lo pidió ESTE navegador? La cookie tiene que traer el mismo state.
    #    Se comprueba ANTES que nada: si no cuadra, ni siquiera canjeamos el
    #    `code` con Google. compare_digest para no filtrar el state por tiempos.
    cookie_state = request.cookies.get(_GLOGIN_STATE_COOKIE)
    if not cookie_state or not secrets.compare_digest(cookie_state, state):
        logger.warning("auth.google_login.state_mismatch", ip=_client_ip(request))
        return _finish_google_login(error="google_state")
    # 2) ¿Sigue vivo el state en Redis? (un solo uso: se borra al consumirlo)
    r = get_redis()
    key = _GLOGIN_STATE_PREFIX + state
    if not await r.get(key):
        return _finish_google_login(error="google_state")
    await r.delete(key)

    try:
        identity = await fetch_identity(code)
    except Exception as e:  # noqa: BLE001 — cualquier fallo de red/Google
        logger.error("auth.google_login.exchange_failed", error=str(e))
        return _finish_google_login(error="google_exchange")

    if not identity.email or not identity.email_verified:
        return _finish_google_login(error="google_unverified")

    ip = _client_ip(request)
    ua = request.headers.get("user-agent")
    user = (
        await db.execute(select(User).where(User.email == identity.email))
    ).scalar_one_or_none()
    if not user or not user.activo:
        await record_audit(
            db,
            user_id=user.id if user else None,
            action="auth.google_login.denied",
            entity="user",
            entity_id=user.id if user else None,
            after={"email": identity.email},
            ip=ip,
            user_agent=ua,
        )
        await db.commit()
        return _finish_google_login(error="google_no_user")

    token = create_access_token(subject=str(user.id), extra={"role": user.role.value})
    user.last_login = func.now()
    await record_audit(
        db,
        user_id=user.id,
        action="auth.google_login.success",
        entity="user",
        entity_id=user.id,
        ip=ip,
        user_agent=ua,
    )
    await db.commit()
    return _finish_google_login(token=token)


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(user)


class ProfileUpdate(BaseModel):
    nombre: str | None = None
    email: EmailStr | None = None
    current_password: str | None = None
    new_password: str | None = None


@router.patch("/me", response_model=UserOut)
async def update_me(
    payload: ProfileUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> UserOut:
    """El propio usuario edita su perfil: nombre, email y contraseña.
    El rol NO se puede cambiar aquí (solo un admin desde Usuarios)."""
    if payload.nombre is not None:
        user.nombre = payload.nombre.strip() or None
    if payload.email and payload.email.lower() != user.email:
        new_email = payload.email.lower()
        other = (
            await db.execute(select(User).where(User.email == new_email, User.id != user.id))
        ).scalar_one_or_none()
        if other:
            raise HTTPException(status.HTTP_409_CONFLICT, "Email ya en uso")
        user.email = new_email
    if payload.new_password:
        if not payload.current_password or not verify_password(payload.current_password, user.password_hash):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "La contraseña actual no es correcta")
        if policy_err := password_policy_error(
            payload.new_password, email=user.email, nombre=user.nombre
        ):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, policy_err)
        try:
            user.password_hash = hash_password(payload.new_password)
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
        # Invalida los JWT emitidos antes de este instante (deps lo comprueba).
        user.password_changed_at = datetime.now(timezone.utc)
    await record_audit(db, user_id=user.id, action="auth.profile_updated", entity="user", entity_id=user.id)
    await db.commit()
    await db.refresh(user)
    return UserOut.model_validate(user)


# --- Ticket efímero para el WebSocket del inbox -----------------------------
# El JWT completo en la query string del WS acaba en logs de proxies/accesos.
# En su lugar: el frontend pide aquí (autenticado por cabecera) un ticket
# opaco de un solo uso y 60s de vida, y abre el WS con ?ticket=... El ticket
# no sirve para nada más y muere al usarse.
_WSTICKET_KEY = "wsticket:{ticket}"
_WSTICKET_TTL = 60


class WsTicketOut(BaseModel):
    ticket: str


@router.post("/ws-ticket", response_model=WsTicketOut)
async def create_ws_ticket(user: User = Depends(get_current_user)) -> WsTicketOut:
    ticket = secrets.token_urlsafe(32)
    await get_redis().set(_WSTICKET_KEY.format(ticket=ticket), str(user.id), ex=_WSTICKET_TTL)
    return WsTicketOut(ticket=ticket)


async def consume_ws_ticket(ticket: str | None) -> str | None:
    """Devuelve el user_id del ticket y lo INVALIDA (un solo uso). None si no
    existe o ya se usó."""
    if not ticket:
        return None
    r = get_redis()
    key = _WSTICKET_KEY.format(ticket=ticket)
    # GETDEL atómico: dos conexiones con el mismo ticket → solo entra una.
    raw = await r.execute_command("GETDEL", key)
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)


@router.post("/logout", response_model=OkResponse)
async def logout(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OkResponse:
    # Revoca el JWT (blocklist en Redis hasta su exp).
    token = get_token_from_request(request)
    if token:
        await revoke_token(token)
    await record_audit(
        db,
        user_id=user.id,
        action="auth.logout",
        entity="user",
        entity_id=user.id,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    return OkResponse()


_PWRESET_KEY = "pwreset:{token}"
_PWRESET_TTL = 3600  # 1 hora


class ForgotPasswordBody(BaseModel):
    email: EmailStr


class ResetPasswordTokenBody(BaseModel):
    token: str
    new_password: str


@router.post("/forgot-password", response_model=OkResponse)
@limiter.limit("5/hour")
async def forgot_password(
    request: Request,
    payload: ForgotPasswordBody,
    db: AsyncSession = Depends(get_db),
) -> OkResponse:
    """Solicita un enlace de restablecimiento. Responde SIEMPRE Ok (no revela si
    el email existe). Si existe y está activo, envía el enlace por email."""
    email = payload.email.lower()
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user and user.activo:
        token = secrets.token_urlsafe(32)
        r = get_redis()
        await r.set(_PWRESET_KEY.format(token=token), str(user.id), ex=_PWRESET_TTL)
        link = f"{settings.FRONTEND_BASE_URL.rstrip('/')}/reset-password?token={token}"
        html = (
            "<p>Hola,</p>"
            f"<p>Has solicitado restablecer tu contraseña en {settings.APP_NAME}. "
            "Pulsa el enlace para elegir una nueva (caduca en 1 hora):</p>"
            f'<p><a href="{link}">Restablecer mi contraseña</a></p>'
            "<p>Si no fuiste tú, ignora este correo.</p>"
        )
        await send_email(
            user.email, f"Restablecer tu contraseña — {settings.APP_NAME}", html
        )
        await record_audit(
            db, user_id=user.id, action="auth.forgot_password", entity="user", entity_id=user.id
        )
        await db.commit()
    return OkResponse()


@router.post("/reset-password", response_model=OkResponse)
@limiter.limit("10/hour")
async def reset_password_with_token(
    request: Request,
    payload: ResetPasswordTokenBody,
    db: AsyncSession = Depends(get_db),
) -> OkResponse:
    r = get_redis()
    key = _PWRESET_KEY.format(token=payload.token)
    raw = await r.get(key)
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El enlace no es válido o ha caducado")
    uid = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
    user = (await db.execute(select(User).where(User.id == uuid.UUID(uid)))).scalar_one_or_none()
    if not user:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El enlace no es válido o ha caducado")
    if policy_err := password_policy_error(
        payload.new_password, email=user.email, nombre=user.nombre
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, policy_err)
    try:
        user.password_hash = hash_password(payload.new_password)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    # Invalida los JWT emitidos antes de este instante (deps lo comprueba).
    user.password_changed_at = datetime.now(timezone.utc)
    await r.delete(key)
    await record_audit(
        db, user_id=user.id, action="auth.password_reset_self", entity="user", entity_id=user.id
    )
    await db.commit()
    return OkResponse()
