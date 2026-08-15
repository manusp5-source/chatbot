"""Secretos de canal cifrados en `channels.credentials_encrypted`.

Por qué existe: hasta ahora el `app_secret` de Meta, el token de página de
Instagram y las claves de Retell vivían en `Channel.config` (JSONB en plano),
aunque la columna cifrada ya estaba en el modelo sin usar. Con el `app_secret`
en la mano cualquiera puede firmar webhooks falsos y saltarse la comprobación
de firma, así que esos valores pasan a un blob JSON cifrado con Fernet — el
mismo mecanismo que ya usan `credentials.value_encrypted` y
`ExternalAPI.credentials_encrypted` (ver `app/services/google_oauth.py`).

Cómo se usa:
  - Para LEER, nunca `ch.config`: usa `channel_config(ch)`, que devuelve la
    config con los secretos ya descifrados encima.
  - Para ESCRIBIR, nunca `ch.config = ...` con secretos dentro: usa
    `apply_channel_values(ch, {...})`, que cifra lo que toca y lo saca de la
    config.

Respaldo durante la transición: si una clave no está en el blob cifrado se lee
del `config` en plano (instalaciones anteriores a la migración 0054). En cuanto
se vuelve a guardar el canal, el valor sale de la config y solo queda cifrado.

Lo que NO se cifra: la `api_key` de webchat, que es pública por diseño (viaja
en el HTML de la web del cliente), y los identificadores que no son secretos
(page_id, phone_number, voice_id, agent_id_retell…).
"""
from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any

from app.core.encryption import get_encryption_service
from app.core.logging import get_logger
from app.models.channel import Channel, ChannelType

logger = get_logger(__name__)


# Donde se aparta un blob cifrado que dejó de poder descifrarse, en base64,
# para no destruirlo al guardar encima. Empieza por guion bajo para que salte a
# la vista que no es un ajuste del canal.
BLOB_ILEGIBLE = "_credenciales_ilegibles_b64"

# Claves de cada tipo de canal que viajan cifradas. Cualquier clave que no esté
# aquí se queda en `config` en plano.
SECRET_KEYS: dict[str, tuple[str, ...]] = {
    ChannelType.instagram_dm.value: ("app_secret", "page_access_token", "verify_token"),
    # El `webhook_secret` de Retell es legado: el panel dejó de pedirlo en
    # ago-2026 (Retell firma con la API key y nunca hubo dónde pegarlo). Se
    # queda en la lista para que el valor de instalaciones antiguas siga
    # leyéndose cifrado y no acabe en claro en `config`.
    ChannelType.retell_voice.value: ("api_key", "webhook_secret"),
}


def secret_keys_for(channel_type: ChannelType | str) -> tuple[str, ...]:
    """Claves secretas del tipo de canal dado. Tupla vacía si no tiene."""
    key = channel_type.value if isinstance(channel_type, ChannelType) else str(channel_type)
    return SECRET_KEYS.get(key, ())


def secrets_unreadable(ch: Channel) -> bool:
    """¿Hay blob pero no se puede descifrar? (clave rotada, dato corrupto).

    Es distinto de "no hay secretos", y hay que distinguirlo SIEMPRE antes de
    escribir: si se confunden, se pisa un ciphertext que todavía se podría
    recuperar restaurando la ENCRYPTION_KEY buena.
    """
    return bool(ch.credentials_encrypted) and read_channel_secrets(ch) == {}


def read_channel_secrets(ch: Channel) -> dict[str, str]:
    """Descifra el blob de secretos del canal.

    Devuelve `{}` si el canal no tiene blob o si no se puede descifrar (clave
    de cifrado cambiada, dato corrupto). No lanza: un canal ilegible tiene que
    caer al respaldo en plano, no tumbar el webhook que lo estaba usando.

    Para ESCRIBIR no basta con esto: un `{}` aquí puede significar "no hay
    nada" o "no se puede leer". Usa `secrets_unreadable` para separarlos.
    """
    blob = ch.credentials_encrypted
    if not blob:
        return {}
    try:
        datos = json.loads(get_encryption_service().decrypt(blob))
    except Exception as e:  # noqa: BLE001 — ver docstring
        logger.warning(
            "channel.secrets.unreadable", channel_id=str(ch.id), error=str(e)
        )
        return {}
    if not isinstance(datos, dict):
        logger.warning("channel.secrets.malformed", channel_id=str(ch.id))
        return {}
    return {k: v for k, v in datos.items() if isinstance(v, str) and v}


def channel_config(ch: Channel) -> dict[str, Any]:
    """La config del canal con los secretos descifrados encima.

    Es lo que debe leer TODO el runtime en lugar de `ch.config`. El valor
    cifrado manda sobre el que pudiera quedar en plano en la config.

    No devuelve `BLOB_ILEGIBLE`: no es un ajuste del canal, es un resto
    guardado para poder rescatarlo.
    """
    cfg = {k: v for k, v in (ch.config or {}).items() if k != BLOB_ILEGIBLE}
    return {**cfg, **read_channel_secrets(ch)}


def apply_channel_values(ch: Channel, valores: Mapping[str, Any]) -> None:
    """Guarda `valores` en el canal, cifrando los que sean secretos.

    - Los secretos se mezclan con los que ya hubiera en el blob (así un
      formulario puede mandar solo los campos que cambian) y se BORRAN de la
      config, incluida cualquier copia en plano que quedara de antes.
    - El resto se mezclan en `config` tal cual.
    - Un valor vacío o `None` no pisa lo que ya había.

    Si el blob de antes NO se puede descifrar, el ciphertext viejo NO se tira:
    se aparta en `config[BLOB_ILEGIBLE]`. Este es el caso de rotar la
    ENCRYPTION_KEY, y el remedio que dice la guía —volver a meter las claves
    por el panel— era justo lo que las destruía: el formulario deja en blanco
    lo que no cambias, así que el `webhook_secret` que no tecleas se perdía
    para siempre aunque luego recuperases la clave buena. Apartado no estorba
    (sin la clave no vale para nada) y permite rescatarlo.

    No hace commit: el llamante decide cuándo.
    """
    secretas = secret_keys_for(ch.type)
    secretos = dict(read_channel_secrets(ch))
    config = dict(ch.config or {})

    if secrets_unreadable(ch):
        anterior = ch.credentials_encrypted
        # Solo se aparta el primero: si ya hay uno guardado, ese es el original
        # y el de ahora es un intento posterior sin valor.
        if BLOB_ILEGIBLE not in config and anterior:
            config[BLOB_ILEGIBLE] = base64.b64encode(bytes(anterior)).decode()
        logger.error(
            "channel.secrets.overwriting_unreadable",
            channel_id=str(ch.id),
            channel_type=getattr(ch.type, "value", str(ch.type)),
            apartado=BLOB_ILEGIBLE in config,
        )

    for k, v in valores.items():
        if k in secretas:
            texto = "" if v is None else str(v)
            if texto:
                secretos[k] = texto
        elif v is not None:
            config[k] = v

    # Los secretos nunca se quedan en la config, ni los que acaban de llegar ni
    # los heredados de una instalación anterior a la migración 0054.
    for k in secretas:
        heredado = config.pop(k, None)
        if isinstance(heredado, str) and heredado and k not in secretos:
            secretos[k] = heredado

    ch.config = config
    ch.credentials_encrypted = (
        get_encryption_service().encrypt(json.dumps(secretos, ensure_ascii=False))
        if secretos
        else None
    )


def rescatar_blob_ilegible(ch: Channel) -> bytes | None:
    """Devuelve el ciphertext apartado por `apply_channel_values`, si lo hay.

    Para rescatar credenciales tras recuperar la ENCRYPTION_KEY buena. No hay
    pantalla para esto: se usa a mano desde una consola, que es cuando toca.
    """
    guardado = (ch.config or {}).get(BLOB_ILEGIBLE)
    if not isinstance(guardado, str) or not guardado:
        return None
    try:
        return base64.b64decode(guardado)
    except Exception:  # noqa: BLE001 — un resto ilegible no vale para nada
        return None
