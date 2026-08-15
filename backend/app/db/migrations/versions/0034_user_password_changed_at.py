"""users.password_changed_at — invalida JWTs emitidos antes del cambio

Revision ID: 0034_user_password_changed_at
Revises: 0033_agent_correction_promoted
Create Date: 2026-07-01

Añade users.password_changed_at (timestamptz, nullable). Al cambiar/resetear la
contraseña se sella este instante; get_current_user rechaza tokens con `iat`
anterior → un token robado deja de valer en cuanto la víctima cambia su clave
(antes seguía siendo válido hasta 24h, su exp).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0034_user_password_changed_at"
down_revision = "0033_agent_correction_promoted"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "users" in set(inspector.get_table_names()):
        cols = {c["name"] for c in inspector.get_columns("users")}
        if "password_changed_at" not in cols:
            op.add_column(
                "users",
                sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "users" in set(inspector.get_table_names()):
        cols = {c["name"] for c in inspector.get_columns("users")}
        if "password_changed_at" in cols:
            op.drop_column("users", "password_changed_at")
