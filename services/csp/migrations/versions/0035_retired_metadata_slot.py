"""Retired metadata revision kept to preserve existing migration histories.

The metadata formerly introduced here is no longer part of the platform
schema. Revision 0035 remains an empty step because revision 0036 and later
revisions depend on its identifier.
"""
from __future__ import annotations

from typing import Sequence, Union


revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
