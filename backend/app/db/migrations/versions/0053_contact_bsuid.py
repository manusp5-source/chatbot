"""Identidad de WhatsApp por BSUID en la ficha del contacto.

Desde abril de 2026 Meta puede NO mandar el teléfono del cliente cuando este
tiene nombre de usuario de WhatsApp. Lo que manda siempre es el BSUID
(Business-Scoped User ID), que además no cambia aunque el cliente se cambie de
número. Hasta ahora ese identificador llegaba, se usaba para inventar un
`telefono` de pega (`wa:<bsuid>`) y se tiraba: la llave real no se guardaba en
ninguna parte, así que el mismo cliente acababa con dos fichas —una con su
teléfono y otra con el `wa:`— sin nada que las relacionara.

Dos columnas nuevas, las dos opcionales y vacías al empezar:

  wa_user_id         el BSUID. ÚNICO: dos fichas no pueden ser el mismo cliente.
  wa_parent_user_id  el de la cuenta padre (carteras de Meta), solo como traza.

No toca ningún dato existente. Las fichas de antes se quedan con
`wa_user_id` a NULL y siguen resolviéndose por teléfono exactamente igual que
hasta ahora; el BSUID se va rellenando solo según van escribiendo.

El índice único es PARCIAL (`WHERE wa_user_id IS NOT NULL`) porque en Postgres
un único normal deja pasar tantos NULL como quieras, pero el parcial además no
ocupa espacio por las miles de fichas sin BSUID (correo, web, Instagram).

Revision ID: 0053_contact_bsuid
Revises: 0052_classifier_rules
"""
import sqlalchemy as sa
from alembic import op

revision = "0053_contact_bsuid"
down_revision = "0052_classifier_rules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("contacts", sa.Column("wa_user_id", sa.String(128), nullable=True))
    op.add_column("contacts", sa.Column("wa_parent_user_id", sa.String(128), nullable=True))
    op.execute(
        "CREATE UNIQUE INDEX ux_contacts_wa_user_id ON contacts (wa_user_id) "
        "WHERE wa_user_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_contacts_wa_user_id")
    op.drop_column("contacts", "wa_parent_user_id")
    op.drop_column("contacts", "wa_user_id")
