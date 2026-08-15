"""Secretos de canal cifrados (`app/services/channel_secrets.py`).

Lo que se protege aquí: que el `app_secret` de Meta, el token de Instagram y
las claves de Retell dejen de estar en `channels.config` en plano, que se sigan
leyendo igual mientras haya instalaciones sin migrar, y que un blob ilegible no
tumbe el webhook que lo estaba usando.

Lógica pura sobre objetos del modelo: no hace falta base de datos.
"""
from __future__ import annotations

import json

from app.core.encryption import EncryptionService, get_encryption_service
from app.models.channel import Channel, ChannelType
from app.services.channel_secrets import (
    BLOB_ILEGIBLE,
    apply_channel_values,
    channel_config,
    read_channel_secrets,
    rescatar_blob_ilegible,
    secret_keys_for,
    secrets_unreadable,
)

# Una segunda clave Fernet válida, para simular que se ha rotado ENCRYPTION_KEY:
# el blob de antes queda perfectamente formado pero ilegible.
OTRA_CLAVE = "0S8yBWn3xU1zVUqIhU4Y5tEHRz7RGB0kk-dOHTZ0Vew="


def _canal(tipo: ChannelType, config: dict | None = None) -> Channel:
    return Channel(type=tipo, name="prueba", enabled=True, config=config or {})


# ---------------------------------------------------------------------------
# Qué se considera secreto
# ---------------------------------------------------------------------------


def test_webchat_no_tiene_secretos():
    """La api_key de webchat es pública por diseño: no se cifra."""
    assert secret_keys_for(ChannelType.webchat) == ()

    ch = _canal(ChannelType.webchat)
    apply_channel_values(ch, {"api_key": "clave-publica"})

    assert ch.credentials_encrypted is None
    assert ch.config["api_key"] == "clave-publica"


def test_secretos_de_instagram_y_retell():
    assert set(secret_keys_for(ChannelType.instagram_dm)) == {
        "app_secret",
        "page_access_token",
        "verify_token",
    }
    assert set(secret_keys_for("retell_voice")) == {"api_key", "webhook_secret"}


# ---------------------------------------------------------------------------
# Escritura
# ---------------------------------------------------------------------------


def test_el_secreto_no_queda_en_la_config():
    ch = _canal(ChannelType.instagram_dm)
    apply_channel_values(
        ch,
        {
            "page_id": "17841400000000000",
            "app_secret": "secreto-de-meta",
            "page_access_token": "IGQVJX-token",
            "verify_token": "verificame",
        },
    )

    # El identificador se queda en claro; los tres secretos, no.
    assert ch.config == {"page_id": "17841400000000000"}
    assert b"secreto-de-meta" not in bytes(ch.credentials_encrypted)
    assert channel_config(ch)["app_secret"] == "secreto-de-meta"
    assert channel_config(ch)["page_access_token"] == "IGQVJX-token"


def test_guardado_parcial_no_borra_los_demas_secretos():
    """El formulario de Retell manda solo lo que cambia."""
    ch = _canal(ChannelType.retell_voice)
    apply_channel_values(ch, {"api_key": "key-1", "webhook_secret": "hmac-1"})

    apply_channel_values(ch, {"api_key": "key-2"})

    cfg = channel_config(ch)
    assert cfg["api_key"] == "key-2"
    assert cfg["webhook_secret"] == "hmac-1"


def test_un_secreto_vacio_no_pisa_el_que_habia():
    ch = _canal(ChannelType.retell_voice)
    apply_channel_values(ch, {"api_key": "key-1", "webhook_secret": "hmac-1"})

    apply_channel_values(ch, {"api_key": "", "voice_id": "eleven-x"})

    cfg = channel_config(ch)
    assert cfg["api_key"] == "key-1"
    assert cfg["voice_id"] == "eleven-x"


def test_al_guardar_se_limpia_el_secreto_heredado_en_plano():
    """Instalación vieja: el secreto estaba en la config y nadie lo mandó de
    nuevo. Al guardar cualquier otra cosa, se cifra y sale de la config."""
    ch = _canal(
        ChannelType.retell_voice,
        {"api_key": "key-vieja", "webhook_secret": "hmac-viejo", "voice_id": "v1"},
    )

    apply_channel_values(ch, {"voice_id": "v2"})

    assert "api_key" not in ch.config
    assert "webhook_secret" not in ch.config
    cfg = channel_config(ch)
    assert cfg["api_key"] == "key-vieja"
    assert cfg["webhook_secret"] == "hmac-viejo"
    assert cfg["voice_id"] == "v2"


# ---------------------------------------------------------------------------
# Lectura
# ---------------------------------------------------------------------------


def test_respaldo_en_plano_mientras_no_se_migre():
    """Antes de la 0054 los secretos siguen en config: hay que leerlos."""
    ch = _canal(ChannelType.instagram_dm, {"app_secret": "en-plano", "page_id": "1"})

    assert ch.credentials_encrypted is None
    assert channel_config(ch)["app_secret"] == "en-plano"


def test_el_cifrado_manda_sobre_el_resto_en_plano():
    ch = _canal(ChannelType.instagram_dm, {"app_secret": "viejo"})
    ch.credentials_encrypted = get_encryption_service().encrypt(
        json.dumps({"app_secret": "nuevo"})
    )

    assert channel_config(ch)["app_secret"] == "nuevo"


def test_blob_ilegible_no_revienta_y_cae_al_respaldo():
    """Clave de cifrado cambiada o dato corrupto: el webhook no puede caerse."""
    ch = _canal(ChannelType.retell_voice, {"api_key": "la-de-siempre"})
    ch.credentials_encrypted = b"esto-no-es-un-token-fernet"

    assert read_channel_secrets(ch) == {}
    assert channel_config(ch)["api_key"] == "la-de-siempre"


# ---------------------------------------------------------------------------
# Clave rotada: el caso que destruía credenciales
# ---------------------------------------------------------------------------


def _canal_con_blob_de_otra_clave(tipo: ChannelType, secretos: dict) -> Channel:
    """Canal cuyo blob se cifró con OTRA ENCRYPTION_KEY: existe y no se lee."""
    ch = _canal(tipo)
    ch.credentials_encrypted = EncryptionService(OTRA_CLAVE).encrypt(
        json.dumps(secretos)
    )
    return ch


def test_blob_de_otra_clave_se_detecta_como_ilegible_no_como_vacio():
    ch = _canal_con_blob_de_otra_clave(ChannelType.retell_voice, {"api_key": "viva"})

    assert read_channel_secrets(ch) == {}   # para leer, no hay nada utilizable
    assert secrets_unreadable(ch) is True   # pero NO es lo mismo que "no hay"

    vacio = _canal(ChannelType.retell_voice)
    assert secrets_unreadable(vacio) is False


def test_guardar_sobre_un_blob_ilegible_no_destruye_el_ciphertext():
    """Rotar ENCRYPTION_KEY y volver a meter las claves por el panel NO puede
    borrar para siempre lo que no has vuelto a teclear."""
    ch = _canal_con_blob_de_otra_clave(
        ChannelType.retell_voice, {"api_key": "KEY-VIVA", "webhook_secret": "HMAC-VIVO"}
    )
    original = bytes(ch.credentials_encrypted)

    # El formulario manda solo la api_key nueva; el webhook_secret va en blanco.
    apply_channel_values(ch, {"api_key": "KEY-NUEVA"})

    assert channel_config(ch)["api_key"] == "KEY-NUEVA"
    # Y el ciphertext de antes sigue ahí, apartado y rescatable.
    assert rescatar_blob_ilegible(ch) == original
    recuperado = json.loads(
        EncryptionService(OTRA_CLAVE).decrypt(rescatar_blob_ilegible(ch))
    )
    assert recuperado["webhook_secret"] == "HMAC-VIVO"


def test_el_resto_apartado_no_se_pisa_con_un_segundo_guardado():
    """El primero es el bueno; los intentos siguientes no valen nada."""
    ch = _canal_con_blob_de_otra_clave(ChannelType.retell_voice, {"api_key": "ORIGINAL"})
    original = bytes(ch.credentials_encrypted)

    apply_channel_values(ch, {"api_key": "intento-1"})
    apply_channel_values(ch, {"api_key": "intento-2"})

    assert rescatar_blob_ilegible(ch) == original


def test_el_resto_apartado_no_sale_como_ajuste_del_canal():
    ch = _canal_con_blob_de_otra_clave(ChannelType.instagram_dm, {"app_secret": "x"})
    apply_channel_values(ch, {"app_secret": "nuevo", "page_id": "1784"})

    assert BLOB_ILEGIBLE in ch.config          # guardado donde toca
    assert BLOB_ILEGIBLE not in channel_config(ch)  # pero no es un ajuste
    assert channel_config(ch)["page_id"] == "1784"
