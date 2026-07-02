"""Add ``is_internal`` flag on ``model_registry``.

Phase 1 of the inference-stack decoupling work (see ANILA root
docs / README §「模型 stack 維護」). When CSP starts reaching models
via docker internal DNS (`http://gemma4:8000/v1`) on a separate
compose project (``anila-models``), we need a way to mark rows whose
endpoint is "private internal-network only" — visually distinct from
"external on-prem LAN endpoint" in the admin UI.

The flag itself doesn't enforce anything at the DB layer; it's a hint
for response shaping and the admin UI lock indicator. Pair with:
  * ``api/models.py::ENDPOINT_INTERNAL`` sentinel — non-owner callers
    see ``<internal>`` instead of the URL.
  * ``ModelsView.vue`` — checkbox in the create / edit form, and a
    lock icon next to the model name when the row is marked.

Schema default is ``FALSE`` so existing rows stay flagged as "external"
(historical LAN-IP endpoints) until an admin flips them at the UI.
The API schema (``schemas/model_registry.py``) overrides this with
``ModelCreate.is_internal = True`` so freshly-registered models default
to internal — fitting the new architecture where everything new goes
on the anila-models-net.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "model_registry",
        sa.Column(
            "is_internal",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("model_registry", "is_internal")
