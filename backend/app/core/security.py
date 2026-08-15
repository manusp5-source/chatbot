import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from jose import JWTError, jwt

from app.core.config import settings
from app.core.redis import get_redis

MAX_PASSWORD_BYTES = 72  # límite duro de bcrypt
PASSWORD_MIN_LENGTH = 12
_REVOKED_PREFIX = "auth:revoked:"

# Contraseñas y patrones que la política anterior (10 caracteres, una letra y un
# dígito) daba por buenos: "contrasena1", "1234567890a", "empresa2024"… El
# atacante no prueba al azar, prueba estas. La lista es corta a propósito: son
# las que aparecen en cualquier diccionario de los primeros mil. Conviene
# añadirle el nombre comercial de la instalación, que es el primero que se
# prueba. Se compara en minúsculas y sin separadores.
_COMMON_PASSWORDS = {
    "password", "passw0rd", "contrasena", "contraseña", "contrasenya",
    "qwerty", "qwertyuiop", "asdfghjkl", "iloveyou", "welcome", "admin",
    "administrador", "letmein", "monkey", "dragon", "football", "sunshine",
    "princess", "abc123", "123456", "1234567", "12345678", "123456789",
    "1234567890", "12345678910", "123123123", "111111", "000000",
    "chatbot", "cambiame", "changeme", "secreto", "hola",
    "bienvenido", "usuario", "invitado", "empresa", "prueba", "test",
}

# Secuencias de teclado/dígitos: si al quitarlas no queda casi nada, la
# contraseña es esa secuencia con adorno ("1234567890a").
_SEQUENCES = (
    "1234567890", "0987654321", "abcdefghij", "qwertyuiop", "asdfghjkl", "zxcvbnm",
)


def _normalize(value: str) -> str:
    """Minúsculas y sin separadores, para comparar 'Contra-Sena_1' con la lista."""
    return "".join(c for c in value.lower() if c.isalnum())


def password_policy_error(
    password: str, *, email: str | None = None, nombre: str | None = None
) -> str | None:
    """Motivo por el que la contraseña NO cumple la política, o None si vale.

    Política para un panel expuesto a internet. La aplican TODOS los puntos
    donde se DEFINE una contraseña (alta de usuario, cambio en perfil, reset por
    token, reset por admin). El máximo de 72 bytes lo sigue vigilando
    hash_password.

    IMPORTANTE — esto NO se comprueba nunca al INICIAR SESIÓN. Endurecer la
    política no puede dejar fuera a quien ya tenía una contraseña más corta:
    sigue entrando igual y solo se le exige la nueva regla el día que la cambie.
    Para avisarle de que la suya se ha quedado corta está `password_is_weak()`,
    que el login puede consultar sin bloquear a nadie.

    `email` y `nombre` son opcionales: si se pasan, se rechaza la contraseña que
    los contenga (el clásico "laura2026" de la cuenta laura@…).
    """
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"La contraseña debe tener al menos {PASSWORD_MIN_LENGTH} caracteres."
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        return "La contraseña debe combinar letras y números."

    norm = _normalize(password)
    if not norm:
        return "La contraseña no puede ser solo símbolos."
    if norm in _COMMON_PASSWORDS:
        return "Esa contraseña está en las listas de las más usadas. Elige otra."
    # "contrasena1", "password2026": la parte de letras es una común.
    letras = "".join(c for c in norm if not c.isdigit())
    if len(letras) >= 4 and letras in _COMMON_PASSWORDS:
        return "Esa contraseña es una palabra muy común con números detrás. Elige otra."
    # Un solo carácter repetido ("aaaaaaaaaaa1").
    if len(set(norm)) <= 3:
        return "La contraseña repite siempre los mismos caracteres. Elige otra."
    for seq in _SEQUENCES:
        for size in range(len(seq), 5, -1):
            for start in range(0, len(seq) - size + 1):
                trozo = seq[start:start + size]
                if trozo in norm and len(norm) - len(trozo) < 4:
                    return (
                        "La contraseña es una secuencia del teclado o de dígitos. "
                        "Elige algo que no se pueda teclear de un tirón."
                    )
    if email:
        local = _normalize(email.split("@", 1)[0])
        if len(local) >= 3 and local in norm:
            return "La contraseña no puede contener tu email."
        dominio = _normalize(email.split("@", 1)[1].split(".", 1)[0]) if "@" in email else ""
        if len(dominio) >= 4 and dominio in norm:
            return "La contraseña no puede contener el dominio de tu email."
    if nombre:
        n = _normalize(nombre)
        if len(n) >= 4 and n in norm:
            return "La contraseña no puede contener tu nombre."
    return None


def password_is_weak(password: str, *, email: str | None = None) -> bool:
    """¿Esta contraseña ya no cumpliría la política de hoy?

    Para AVISAR (no para bloquear) a quien entra con una contraseña anterior al
    endurecimiento. Nunca se usa para denegar el acceso.
    """
    return password_policy_error(password, email=email) is not None


def hash_password(password: str) -> str:
    pwd_bytes = password.encode("utf-8")
    if len(pwd_bytes) > MAX_PASSWORD_BYTES:
        raise ValueError(
            f"La contraseña excede {MAX_PASSWORD_BYTES} bytes; "
            "bcrypt no soporta más y truncar es inseguro."
        )
    return bcrypt.hashpw(pwd_bytes, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    pwd_bytes = plain.encode("utf-8")
    if len(pwd_bytes) > MAX_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(pwd_bytes, hashed.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(subject: str, extra: dict[str, Any] | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=settings.JWT_EXPIRATION_HOURS)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError as e:
        raise ValueError(f"Invalid token: {e}") from e


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def revoke_token(token: str) -> None:
    """Añade el token a la blocklist Redis hasta su `exp`."""
    try:
        payload = decode_token(token)
    except ValueError:
        return
    exp = int(payload.get("exp", 0))
    now = int(datetime.now(timezone.utc).timestamp())
    ttl = max(1, exp - now)
    client = get_redis()
    await client.set(f"{_REVOKED_PREFIX}{_token_fingerprint(token)}", "1", ex=ttl)


async def is_token_revoked(token: str) -> bool:
    client = get_redis()
    return bool(await client.exists(f"{_REVOKED_PREFIX}{_token_fingerprint(token)}"))


# ---------------------------------------------------------------------------
# Bloqueo de cuenta por intentos fallidos
#
# Complementa al rate-limit por IP del login: sin él, un ataque distribuido
# prueba contraseñas de una misma cuenta desde muchas IPs sin toparse nunca con
# el límite. El contador vive en Redis, por email.
#
# FALLAR CERRADO. Antes, si Redis no respondía, `_login_failures` devolvía 0 y
# el bloqueo desaparecía: quien pudiera tirar Redis se quedaba con intentos
# ilimitados justo cuando nadie estaba mirando. Ahora, si no se puede consultar
# el contador, el login se rechaza con 503 y se avisa. No deja a nadie tirado de
# más: sin Redis el panel ya no funciona (la comprobación de tokens revocados
# también va contra Redis), así que la alternativa no era "seguir trabajando",
# era "seguir trabajando sin freno para el atacante".
# ---------------------------------------------------------------------------

LOGIN_FAIL_KEY = "login:fail:{email}"
LOGIN_FAIL_MAX = 10
LOGIN_FAIL_WINDOW = 15 * 60  # 15 minutos


class LoginCounterUnavailable(Exception):
    """No se puede consultar el contador de fallos (Redis caído)."""


async def login_failures(email: str) -> int:
    """Fallos acumulados de esa cuenta. Lanza si no se puede saber."""
    try:
        raw = await get_redis().get(LOGIN_FAIL_KEY.format(email=email))
    except Exception as e:
        raise LoginCounterUnavailable(str(e)) from e
    try:
        return int(raw) if raw else 0
    except (TypeError, ValueError):
        return 0


async def register_login_failure(email: str) -> None:
    try:
        r = get_redis()
        key = LOGIN_FAIL_KEY.format(email=email)
        count = await r.incr(key)
        if count == 1:
            await r.expire(key, LOGIN_FAIL_WINDOW)
    except Exception:
        # Si aquí falla no pasa nada: la próxima lectura del contador fallará
        # también y el login se cortará en seco por `login_failures`.
        pass


async def clear_login_failures(email: str) -> None:
    try:
        await get_redis().delete(LOGIN_FAIL_KEY.format(email=email))
    except Exception:
        pass
