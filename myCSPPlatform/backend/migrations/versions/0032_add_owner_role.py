"""Promote the bootstrap admin to the new ``owner`` tier.

Introduces a fourth role tier above admin. Tier order (high → low):

    owner > admin > developer ≈ user

``owner`` gates platform-altering / irreversible operations:
  * promoting / demoting / deleting admin or owner accounts
  * editing or deactivating auth providers (OIDC / SSO)
  * hard-purge endpoints (model registry, agent credentials)
  * viewing raw audit-log fields (IP address, request metadata)
  * seeing the registered model's actual endpoint URL

``admin`` keeps every other moderation surface — billing, usage stats,
day-to-day user CRUD, agent registration. The split is documented in
``app/services/auth_service.py`` (require_admin vs require_owner).

Bootstrap policy: the existing baseline admin (``users.id = 1``,
created by 0001_initial_schema as the seeded ``admin`` account) is
promoted to ``owner`` so the upgrade lands without manual intervention.
If the row was deleted or the username changed, the migration is a
no-op — operators can still manually flip a row via SQL after the fact.

The ``role`` column stays a free-form ``String(20)`` (no DB-level CHECK
constraint) — validation lives at the API boundary
(``schemas/platform_link.py::_ALLOWED_ROLES``). Adding a CHECK now would
fight the existing 'system' role used by the ingestion-worker seed and
make future role additions a migration each time.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    # Idempotent: only flip rows that are still 'admin' AND look like the
    # baseline bootstrap admin. Username 'admin' is the seed value used
    # by the initial schema; matching that protects against accidentally
    # promoting a user-created account that happens to be id=1.
    bind.execute(
        sa.text(
            """
            UPDATE users
               SET role = 'owner'
             WHERE id = 1
               AND username = 'admin'
               AND role = 'admin'
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE users
               SET role = 'admin'
             WHERE id = 1
               AND username = 'admin'
               AND role = 'owner'
            """
        )
    )
