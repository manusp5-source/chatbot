"""Saca los secretos de canal de `channels.config` y los deja cifrados.

Hasta ahora el `app_secret` de Meta, el token de página de Instagram, el
`verify_token` y las claves de Retell (`api_key`, `webhook_secret`) vivían en
`channels.config`, que es un JSONB en plano, mientras la columna
`credentials_encrypted` estaba en el modelo sin que la usara nadie. Con el
`app_secret` en la mano se pueden firmar webhooks falsos y saltarse la
comprobación de firma, así que esos valores pasan al blob cifrado con Fernet
—el mismo mecanismo de `credentials.value_encrypted`— y desaparecen de la
config.

Qué NO se toca: la `api_key` de webchat (es pública por diseño, viaja en el
HTML de la web del cliente), las credenciales de WhatsApp (ya viven cifradas en
la tabla `credentials`) y los identificadores que no son secretos (page_id,
phone_number, voice_id, agent_id_retell, greeting…).

La migración es de datos y necesita `ENCRYPTION_KEY`: es la misma que ya exige
la 0002 para el cifrado de datos personales, así que si la app arranca, esto
también pasa. El código lee con respaldo en plano (`channel_config`), de modo
que una instalación a medio migrar sigue funcionando.

El downgrade devuelve los secretos a la config en plano, para poder volver
atrás sin perder ninguna credencial.

Revision ID: 0054_channel_secrets_encrypted
Revises: 0053_contact_bsuid
"""
import json

from alembic import op
from sqlalchemy.sql import text

revision = "0054_channel_secrets_encrypted"
down_revision = "0053_contact_bsuid"
branch_labels = None
depends_on = None


# Mismo mapa que `app/services/channel_secrets.SECRET_KEYS`. Se repite aquí a
# propósito: una migración tiene que seguir haciendo lo mismo dentro de un año,
# aunque la lista del código haya cambiado.
SECRETOS_POR_CANAL = {
    "instagram_dm": ("app_secret", "page_access_token", "verify_token"),
    "retell_voice": ("api_key", "webhook_secret"),
}


def _canales(bind):
    return bind.execute(
        text(
            "SELECT id, type::text, config, credentials_encrypted FROM channels "
            "WHERE type::text IN ('instagram_dm', 'retell_voice')"
        )
    ).fetchall()


def _guardar(bind, channel_id, config, blob) -> None:
    bind.execute(
        text(
            "UPDATE channels SET config = CAST(:cfg AS JSONB), "
            "credentials_encrypted = :blob WHERE id = :id"
        ),
        {"cfg": json.dumps(config, ensure_ascii=False), "blob": blob, "id": channel_id},
    )


def upgrade() -> None:
    from app.core.encryption import get_encryption_service

    enc = get_encryption_service()
    bind = op.get_bind()

    for channel_id, tipo, config, blob_actual in _canales(bind):
        config = dict(config or {})
        secretos = {}
        # Respeta lo que ya hubiera cifrado (instalación a medio migrar).
        if blob_actual:
            try:
                previos = json.loads(enc.decrypt(bytes(blob_actual)))
            except Exception:  # noqa: BLE001
                # No se puede descifrar (¿ENCRYPTION_KEY rotada?). Este canal se
                # deja EXACTAMENTE como está: reconstruir el blob desde el plano
                # borraría un ciphertext que aún se puede rescatar con la clave
                # buena, y encima pasaría en el despliegue, sin nadie mirando.
                print(
                    f"[0054] Canal {channel_id} ({tipo}): credenciales cifradas "
                    "ilegibles, se deja intacto. Revisa la ENCRYPTION_KEY."
                )
                continue
            if isinstance(previos, dict):
                secretos.update({k: v for k, v in previos.items() if isinstance(v, str)})

        movidos = []
        for clave in SECRETOS_POR_CANAL[tipo]:
            valor = config.pop(clave, None)
            if isinstance(valor, str) and valor:
                secretos[clave] = valor
                movidos.append(clave)

        if not movidos:
            continue

        _guardar(
            bind,
            channel_id,
            config,
            enc.encrypt(json.dumps(secretos, ensure_ascii=False)),
        )


def downgrade() -> None:
    from app.core.encryption import get_encryption_service

    enc = get_encryption_service()
    bind = op.get_bind()

    for channel_id, tipo, config, blob in _canales(bind):
        if not blob:
            continue
        try:
            secretos = json.loads(enc.decrypt(bytes(blob)))
        except Exception:  # noqa: BLE001 — si no se puede leer, no hay qué devolver
            continue
        if not isinstance(secretos, dict):
            continue
        config = dict(config or {})
        for clave in SECRETOS_POR_CANAL[tipo]:
            valor = secretos.get(clave)
            if isinstance(valor, str) and valor:
                config[clave] = valor
        _guardar(bind, channel_id, config, None)
