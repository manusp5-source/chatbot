from collections.abc import Awaitable, Callable
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token, is_token_revoked
from app.db.session import get_db
from app.models.user import User, UserRole

bearer_scheme = HTTPBearer(auto_error=False)


def get_token_from_request(request: Request) -> str | None:
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return None


async def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    # Solo se acepta el token por cabecera Authorization. Aceptarlo por
    # query-string en HTTP filtra el token a logs / Referer / proxies.
    # El WebSocket del inbox usa tickets efímeros (POST /auth/ws-ticket).
    token: str | None = creds.credentials if creds else None

    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token requerido")

    try:
        payload = decode_token(token)
    except ValueError as e:
        # Mensaje genérico: el detalle de python-jose ("Signature verification
        # failed…") describe internals que no aportan nada al cliente legítimo.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido o caducado") from e

    if await is_token_revoked(token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token revocado")

    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido")

    user = (await db.execute(select(User).where(User.id == UUID(sub)))).scalar_one_or_none()
    if not user or not user.activo:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Usuario no válido")
    if _issued_before_password_change(payload, user):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token revocado")
    return user


def _issued_before_password_change(payload: dict, user: User) -> bool:
    """True si el token se emitió ANTES del último cambio de contraseña.

    Un token robado deja de valer en cuanto la víctima cambia su clave: los
    endpoints que cambian/resetean contraseña sellan `password_changed_at` y
    aquí se rechaza cualquier `iat` anterior. Comparación en epoch (int) para
    que un login inmediatamente posterior (mismo segundo) siga siendo válido.
    """
    changed_at = user.password_changed_at
    if not changed_at:
        return False
    iat = payload.get("iat")
    if not isinstance(iat, (int, float)):
        return True  # sin iat no podemos situarlo: fuera
    return int(iat) < int(changed_at.timestamp())


def require_role(*roles: UserRole) -> Callable[[User], Awaitable[User]]:
    async def _checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Permisos insuficientes")
        return user
    return _checker


# ---------------------------------------------------------------------------
# MAPA DE ROLES (leer antes de tocar una dependencia de un endpoint)
#
# Solo hay dos roles, y la diferencia NO es "ve más / ve menos": es qué puede
# hacer. El criterio, fijo:
#
#   admin    → lo irreversible, lo masivo y lo que cuesta dinero.
#              Borrar contactos, borrar etiquetas (afecta a todas las
#              conversaciones), volcar toda la base de contactos a CSV,
#              importar en bloque, archivar en lote, sacar del antispam
#              (re-dispara el bot), escribir en la base de conocimiento y
#              todo /admin/*.
#
#              PENDIENTE en api/knowledge_base.py: LEER la base de conocimiento
#              y `POST /kb/search` (que gasta embeddings de pago) siguen
#              abiertos a cualquier sesión. Ver el test marcado xfail en
#              tests/test_roles_permisos_http.py.
#
#   cliente  → operador de bandeja. El día a día de una conversación:
#              leer, responder, adjuntar, tomar el control, devolver al bot,
#              archivar/desarchivar UNA conversación, cerrar, marcar leído,
#              redactar/afinar/descartar borradores, ver y editar la ficha de
#              un contacto, sus notas, su actividad y sus etiquetas.
#
# `get_current_user` = "hay una sesión válida" y equivale hoy a rol operador.
# Se usa tal cual en los endpoints del día a día; `require_admin` marca el
# resto. No existe una dependencia `require_cliente`: con dos roles sería un
# sinónimo de `get_current_user` y era código muerto (nadie la usaba), así que
# se ha quitado en vez de dar la falsa sensación de que algo la comprobaba.
# ---------------------------------------------------------------------------

require_admin = require_role(UserRole.admin)
