"""Política de contraseñas (core/security.py) y bloqueo de cuenta.

La política anterior — 10 caracteres, una letra y un dígito — daba por buenas
"contrasena1" y "1234567890a", que son las primeras que prueba cualquiera. No
había lista de las más usadas ni comprobación contra el propio email del
usuario.

Regla que NO se puede romper: endurecer la política no deja fuera a nadie. Solo
se aplica cuando se DEFINE una contraseña; quien ya tenía una más corta entra
igual y solo se le exige lo nuevo el día que la cambie.
"""
from __future__ import annotations

import pytest


def test_se_rechazan_las_de_toda_la_vida():
    from app.core.security import password_policy_error

    for mala in (
        "contrasena1",     # palabra común + dígito (pasaba antes)
        "1234567890a",     # secuencia de dígitos + letra (pasaba antes)
        "password1234",
        "qwertyuiop12",
        "aaaaaaaaaaa1",
        "chatbot2026",     # el nombre del producto + año
    ):
        assert password_policy_error(mala) is not None, f"'{mala}' se sigue aceptando"


def test_no_puede_contener_el_email_ni_el_nombre():
    from app.core.security import password_policy_error

    assert password_policy_error("Laura2026Panel", email="laura@ejemplo.com") is not None
    assert password_policy_error("MiEjemploSegura9", email="hola@ejemplo.com") is not None
    assert password_policy_error("MartaRuizClave42", nombre="Marta Ruiz") is not None


def test_se_aceptan_las_razonables():
    from app.core.security import password_policy_error

    for buena in (
        "MiClaveSegura123",
        "trebol-cuchara-9-limon",
        "N0che$DeVerano77",
    ):
        assert password_policy_error(buena, email="laura@ejemplo.com") is None, buena


def test_la_longitud_minima_ha_subido():
    from app.core.security import PASSWORD_MIN_LENGTH, password_policy_error

    assert PASSWORD_MIN_LENGTH >= 12
    assert password_policy_error("Corta12345") is not None  # 10 caracteres


def test_la_politica_no_se_aplica_al_iniciar_sesion():
    """Lo importante: un admin con una contraseña vieja y corta sigue entrando.

    `verify_password` es lo único que decide el login; la política solo se
    consulta al definir una contraseña nueva. `password_is_weak` existe para
    AVISAR, nunca para denegar.
    """
    from app.core.security import hash_password, password_is_weak, verify_password

    vieja = "corta1234a"  # no cumpliría la política de hoy
    h = hash_password(vieja)
    assert verify_password(vieja, h), "el endurecimiento ha dejado fuera a quien ya tenía cuenta"
    assert password_is_weak(vieja) is True


@pytest.mark.asyncio
async def test_el_bloqueo_de_cuenta_falla_cerrado_si_redis_no_responde(monkeypatch):
    """Antes, sin Redis el contador devolvía 0 y el bloqueo desaparecía: quien
    pudiera tirar Redis se quedaba con intentos ilimitados."""
    from app.core import security

    class _RedisMuerto:
        async def get(self, *a, **k):
            raise ConnectionError("redis caído")

    monkeypatch.setattr(security, "get_redis", lambda: _RedisMuerto())
    with pytest.raises(security.LoginCounterUnavailable):
        await security.login_failures("alguien@test.local")


@pytest.mark.asyncio
async def test_el_login_devuelve_503_en_vez_de_barra_libre(monkeypatch):
    """Y el endpoint lo traduce a un 503 claro, no a "adelante"."""
    from fastapi import HTTPException

    from app.api import auth
    from app.core import security

    class _RedisMuerto:
        async def get(self, *a, **k):
            raise ConnectionError("redis caído")

    monkeypatch.setattr(security, "get_redis", lambda: _RedisMuerto())

    class _Payload:
        email = "alguien@test.local"
        password = "loquesea"

    with pytest.raises(HTTPException) as exc:
        await auth.login.__wrapped__(  # el decorador del rate-limit envuelve la función
            request=_FakeRequest(), payload=_Payload(), db=None
        )
    assert exc.value.status_code == 503


class _FakeRequest:
    headers: dict = {}
    client = None
