"""users.last_login — última vez que el usuario entró

Revision ID: 0032_user_last_login
Revises: 0031_correction_learning
Create Date: 2026-06-23

Añade users.last_login (timestamptz, nullable). Se actualiza en cada login
correcto (contraseña o Google) y se muestra en la lista de usuarios del panel.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0032_user_last_login"
down_revision = "0031_correction_learning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "users" in set(inspector.get_table_names()):
        cols = {c["name"] for c in inspector.get_columns("users")}
        if "last_login" not in cols:
            op.add_column(
                "users",
                sa.Column("last_login", sa.DateTime(timezone=True), nullable=True),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "users" in set(inspector.get_table_names()):
        cols = {c["name"] for c in inspector.get_columns("users")}
        if "last_login" in cols:
            op.drop_column("users", "last_login")
