"""Initial Agtyle schema.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-08-09

The physical schema is declared once in ``agtyle.adapters.persistence.models`` and this
initial revision snapshots it. Later revisions must use explicit Alembic operations so that
they stay frozen in time; only this first revision may create the schema from metadata.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from agtyle.adapters.persistence.models import (
    DROP_EVENT_IMMUTABILITY_TRIGGERS,
    EVENT_IMMUTABILITY_TRIGGERS,
    metadata,
)

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    metadata.create_all(bind=bind, checkfirst=False)
    for statement in EVENT_IMMUTABILITY_TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    for statement in DROP_EVENT_IMMUTABILITY_TRIGGERS:
        op.execute(statement)
    op.get_bind()
    metadata.drop_all(bind=op.get_bind(), checkfirst=False)
