"""Tests básicos del módulo de seguridad."""
import pytest

from app.core.security import (
    MAX_PASSWORD_BYTES,
    create_access_token,
    decode_token,
    hash_password,
    verify_password,
)


def test_password_hash_and_verify():
    h = hash_password("MiClave123!")
    assert h != "MiClave123!"
    assert verify_password("MiClave123!", h)
    assert not verify_password("incorrecta", h)


def test_password_too_long_rejected():
    with pytest.raises(ValueError):
        hash_password("a" * (MAX_PASSWORD_BYTES + 1))


def test_verify_password_rejects_overlong():
    h = hash_password("corta")
    assert not verify_password("a" * (MAX_PASSWORD_BYTES + 1), h)


def test_jwt_roundtrip():
    token = create_access_token(subject="usuario-1", extra={"role": "admin"})
    payload = decode_token(token)
    assert payload["sub"] == "usuario-1"
    assert payload["role"] == "admin"
    assert "jti" in payload


def test_jwt_rejects_garbage():
    with pytest.raises(ValueError):
        decode_token("esto-no-es-un-jwt")


def test_password_policy_rejects_weak():
    from app.core.security import PASSWORD_MIN_LENGTH, password_policy_error

    # Corta.
    assert password_policy_error("a1") is not None
    assert password_policy_error("a" * (PASSWORD_MIN_LENGTH - 2) + "1") is not None
    # Sin números / sin letras (aunque sean largas).
    assert password_policy_error("soloLetrasLargas") is not None
    assert password_policy_error("1234567890123") is not None


def test_password_policy_accepts_valid():
    from app.core.security import password_policy_error

    assert password_policy_error("MiClaveSegura123") is None
