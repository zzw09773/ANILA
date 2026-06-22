"""Fix document_relations dst FK: ON DELETE SET NULL → CASCADE.

The composite dst FK ``fk_docrel_dst`` (collection_id, dst_document_id) →
ingestion_documents(collection_id, id) was created with ``ON DELETE SET NULL``
(migration 0039). But ``document_relations.collection_id`` is ``NOT NULL``, so
when a referenced document is deleted — e.g. the CASCADE that fires when an
ingestion_collection is deleted — PostgreSQL tries to set BOTH composite
columns (collection_id, dst_document_id) to NULL, violating the NOT NULL on
collection_id. The whole DELETE aborts → the API returns 500 ("delete
collection" / "delete document").

CASCADE is both the fix and the correct semantics: a relation edge that points
at a now-deleted document should be removed, not kept with a dangling/NULL dst.
"""

from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0046"
down_revision: Union[str, None] = "0045"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

_FK = "fk_docrel_dst"
_TABLE = "document_relations"
_REF = "ingestion_documents"
_LOCAL = ["collection_id", "dst_document_id"]
_REMOTE = ["collection_id", "id"]


def upgrade() -> None:
    op.drop_constraint(_FK, _TABLE, type_="foreignkey")
    op.create_foreign_key(
        _FK, _TABLE, _REF, _LOCAL, _REMOTE, ondelete="CASCADE"
    )


def downgrade() -> None:
    op.drop_constraint(_FK, _TABLE, type_="foreignkey")
    op.create_foreign_key(
        _FK, _TABLE, _REF, _LOCAL, _REMOTE, ondelete="SET NULL"
    )
