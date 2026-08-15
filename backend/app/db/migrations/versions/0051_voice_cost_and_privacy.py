"""Voz: coste de los minutos + transcripción y grabación cifradas.

Tres cosas, todas del canal Retell:

1. `conversations.call_cost_usd` (+ `call_cost_source`). El coste de TELEFONÍA
   no se guardaba en ninguna parte: el tope mensual de gasto medía tokens del
   modelo y se dejaba fuera la mitad cara del canal más caro. Los minutos se
   facturan por tiempo, no por tokens, así que no caben en `llm_model_price`
   (tarifas por millón de tokens): van como coste PLANO por conversación.
   `call_cost_source` distingue el importe real de Retell de la estimación por
   tarifa — un 0.0 facturado y un 0.0 "no tengo tarifa" son la misma cifra con
   significados opuestos.

2. `conversations.call_recording_url` pasa de TEXT a BYTEA cifrado. Mientras
   `opt_in_signed_url` no esté activo en el agente de Retell, esa URL es pública
   y sin caducidad: guardarla en claro era guardar el audio del cliente en
   claro. El único predicado SQL que la tocaba es `IS NOT NULL`, que funciona
   igual sobre BYTEA.

3. Las transcripciones de llamada YA GUARDADAS se mueven de `messages.contenido`
   (texto plano) a `messages.audio_transcript` (cifrado). En una llamada,
   `contenido` era literalmente la voz del cliente transcrita: el mismo dato que
   en una nota de voz de WhatsApp ya iba cifrado. Solo se mueven los mensajes
   del cliente (`rol='user'`) de conversaciones `retell_voice`; lo que dijo el
   bot se queda donde estaba.

Idempotente: se puede aplicar dos veces sin romper. Reversible: el downgrade
descifra y devuelve los datos a su sitio.

CORRE EN PRODUCCIÓN AL DESPLEGAR. El cifrado va fila a fila en Python (Fernet
no se puede hacer desde SQL) y en lotes, y solo toca filas del canal de voz.

Revision ID: 0051_voice_cost_and_privacy
Revises: 0050_outbound_optout
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql import text

revision = "0051_voice_cost_and_privacy"
down_revision = "0050_outbound_optout"
branch_labels = None
depends_on = None

_BATCH = 500


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table(table):
        return False
    return column in {c["name"] for c in insp.get_columns(table)}


def _column_type(table: str, column: str) -> str:
    bind = op.get_bind()
    for c in sa.inspect(bind).get_columns(table):
        if c["name"] == column:
            return str(c["type"]).upper()
    return ""


# ---------------------------------------------------------------------------
# 1) Coste de telefonía
# ---------------------------------------------------------------------------


def _add_cost_columns() -> None:
    if not _has_column("conversations", "call_cost_usd"):
        op.add_column(
            "conversations", sa.Column("call_cost_usd", sa.Numeric(10, 4), nullable=True)
        )
    if not _has_column("conversations", "call_cost_source"):
        op.add_column(
            "conversations", sa.Column("call_cost_source", sa.String(16), nullable=True)
        )


def _drop_cost_columns() -> None:
    if _has_column("conversations", "call_cost_source"):
        op.drop_column("conversations", "call_cost_source")
    if _has_column("conversations", "call_cost_usd"):
        op.drop_column("conversations", "call_cost_usd")


# ---------------------------------------------------------------------------
# 2) URL de la grabación: TEXT -> BYTEA cifrado
# ---------------------------------------------------------------------------


def _encrypt_recording_url() -> None:
    """Mismo patrón que 0002_encrypt_pii: columna temporal, cifrar, borrar."""
    if not _has_column("conversations", "call_recording_url"):
        return
    if "BYTEA" in _column_type("conversations", "call_recording_url"):
        return  # ya cifrada (re-ejecución)

    from app.core.encryption import get_encryption_service

    bind = op.get_bind()
    op.alter_column(
        "conversations", "call_recording_url", new_column_name="call_recording_url_plain"
    )
    op.add_column("conversations", sa.Column("call_recording_url", sa.LargeBinary(), nullable=True))

    enc = get_encryption_service()
    rows = bind.execute(
        text(
            "SELECT id, call_recording_url_plain FROM conversations "
            "WHERE call_recording_url_plain IS NOT NULL"
        )
    ).fetchall()
    for row in rows:
        bind.execute(
            text("UPDATE conversations SET call_recording_url = :ct WHERE id = :id"),
            {"ct": enc.encrypt(row[1]), "id": row[0]},
        )
    op.drop_column("conversations", "call_recording_url_plain")


def _decrypt_recording_url() -> None:
    if not _has_column("conversations", "call_recording_url"):
        return
    if "BYTEA" not in _column_type("conversations", "call_recording_url"):
        return  # ya está en claro

    from app.core.encryption import get_encryption_service

    bind = op.get_bind()
    op.alter_column(
        "conversations", "call_recording_url", new_column_name="call_recording_url_enc"
    )
    op.add_column("conversations", sa.Column("call_recording_url", sa.Text(), nullable=True))

    enc = get_encryption_service()
    rows = bind.execute(
        text(
            "SELECT id, call_recording_url_enc FROM conversations "
            "WHERE call_recording_url_enc IS NOT NULL"
        )
    ).fetchall()
    for row in rows:
        try:
            plain = enc.decrypt(bytes(row[1]))
        except Exception:
            plain = None
        if plain is None:
            # Sin la clave correcta no podemos devolver la URL a claro, y el
            # drop_column de abajo se llevaria la version cifrada por delante:
            # la grabacion quedaria inalcanzable. Se aborta antes de borrar.
            raise RuntimeError(
                f"0051 downgrade: no se puede descifrar la URL de grabacion de la "
                f"conversacion {row[0]}. La ENCRYPTION_KEY actual no coincide con "
                "la que se uso al cifrar. Se aborta sin borrar nada: restaura la "
                "clave correcta y vuelve a intentarlo."
            )
        bind.execute(
            text("UPDATE conversations SET call_recording_url = :pt WHERE id = :id"),
            {"pt": plain, "id": row[0]},
        )
    op.drop_column("conversations", "call_recording_url_enc")


# ---------------------------------------------------------------------------
# 3) Transcripciones de llamada: contenido (claro) -> audio_transcript (cifrado)
# ---------------------------------------------------------------------------

_VOICE_PLAINTEXT_SQL = """
    SELECT m.id, m.contenido
    FROM messages m
    JOIN conversations c ON c.id = m.conversation_id
    WHERE c.canal = 'retell_voice'
      AND m.rol = 'user'
      AND m.contenido IS NOT NULL
      AND m.audio_transcript IS NULL
    ORDER BY m.id
    LIMIT :lim
"""


def _move_voice_transcripts_to_encrypted() -> int:
    """Mueve la voz del cliente al campo cifrado. Devuelve cuántas filas movió.

    En lotes y siempre releyendo el primer lote pendiente: cada fila procesada
    deja de cumplir el WHERE (contenido pasa a NULL), así que el bucle avanza
    solo y una re-ejecución no duplica nada.
    """
    from app.core.encryption import get_encryption_service

    bind = op.get_bind()
    enc = get_encryption_service()
    movidas = 0
    while True:
        rows = bind.execute(text(_VOICE_PLAINTEXT_SQL), {"lim": _BATCH}).fetchall()
        if not rows:
            break
        for row in rows:
            bind.execute(
                text(
                    "UPDATE messages SET audio_transcript = :ct, contenido = NULL "
                    "WHERE id = :id"
                ),
                {"ct": enc.encrypt(row[1]), "id": row[0]},
            )
            movidas += 1
        if len(rows) < _BATCH:
            break
    return movidas


_VOICE_ENCRYPTED_SQL = """
    SELECT m.id, m.audio_transcript
    FROM messages m
    JOIN conversations c ON c.id = m.conversation_id
    WHERE c.canal = 'retell_voice'
      AND m.rol = 'user'
      AND m.contenido IS NULL
      AND m.audio_transcript IS NOT NULL
      AND m.audio_url IS NULL
    ORDER BY m.id
    LIMIT :lim
"""


def _move_voice_transcripts_back() -> int:
    """Downgrade: descifra y devuelve la transcripción a `contenido`.

    Excluye los mensajes con `audio_url`: esos son notas de voz de verdad, cuya
    transcripción vivía cifrada desde 0002 y no la puso esta migración.
    """
    from app.core.encryption import get_encryption_service

    bind = op.get_bind()
    enc = get_encryption_service()
    movidas = 0
    while True:
        rows = bind.execute(text(_VOICE_ENCRYPTED_SQL), {"lim": _BATCH}).fetchall()
        if not rows:
            break
        avanzó = False
        for row in rows:
            try:
                plain = enc.decrypt(bytes(row[1]))
            except Exception:
                plain = None
            if plain is None:
                # La ENCRYPTION_KEY actual no es la que cifró este dato.
                # Antes aquí se escribía cadena vacía y se borraba el cifrado,
                # o sea que la vuelta atrás DESTRUÍA la transcripción justo
                # cuando más falta hace: el downgrade es el camino de
                # emergencia. Se aborta para no perder nada.
                raise RuntimeError(
                    f"0051 downgrade: no se puede descifrar la transcripcion del "
                    f"mensaje {row[0]}. La ENCRYPTION_KEY actual no coincide con "
                    "la que se uso al cifrar. Se aborta sin tocar el dato: "
                    "restaura la clave correcta y vuelve a intentarlo."
                )
            bind.execute(
                text(
                    "UPDATE messages SET contenido = :pt, audio_transcript = NULL "
                    "WHERE id = :id"
                ),
                {"pt": plain, "id": row[0]},
            )
            movidas += 1
            avanzó = True
        if not avanzó or len(rows) < _BATCH:
            break
    return movidas


def upgrade() -> None:
    _add_cost_columns()
    _encrypt_recording_url()
    _move_voice_transcripts_to_encrypted()


def downgrade() -> None:
    _move_voice_transcripts_back()
    _decrypt_recording_url()
    _drop_cost_columns()
