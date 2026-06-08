"""Row-level security on ``ingestion_images`` (defense-in-depth, S-116).

Mirrors the ``document_chunks`` RLS introduced in 0019: ENABLE + FORCE row
security, with a policy keyed on the ``anila.collection_id`` GUC so the engine
filters rows even when application code forgets to scope. The image
search / blob / ingest paths set ``SET LOCAL anila.collection_id = N`` inside a
transaction (see app/api/ingestion/search.py, image_blob.py and the
ingestion-worker handlers).

The blob endpoint is a by-PK lookup (``WHERE id = N``) that needs the row to
*learn* its collection_id — a chicken-and-egg under FORCE RLS (no GUC → no row →
can't learn the collection). We resolve it with a narrow ``SECURITY DEFINER``
function ``ingestion_image_collection_id(id)`` that returns ONLY the
collection_id (no sensitive columns), owned by the migration superuser so it
bypasses RLS. The blob endpoint then enforces ownership via
``_require_collection_access`` and re-reads the row under a matching GUC.

NOTE (deploy): ingestion_images RLS is defense-in-depth — the image search /
blob endpoints already gate on collection ownership. Run the image
search / blob / ingest integration tests against Postgres before deploying;
SQLite cannot exercise RLS.

Revision ID: 0037
Revises: 0036
Create Date: 2026-06-03
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0037"
down_revision: Union[str, None] = "0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. Enable + force RLS (owner is subject too — same as chunks) ────────
    op.execute("ALTER TABLE ingestion_images ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE ingestion_images FORCE ROW LEVEL SECURITY")

    # ── 2. Collection-scoped policy (identical shape to chunks_collection_*) ──
    # FOR ALL with USING only → INSERT reuses the USING expr as its WITH CHECK,
    # so writers must SET LOCAL anila.collection_id = <row's collection_id>.
    op.execute(
        """
        CREATE POLICY images_collection_isolation ON ingestion_images
            FOR ALL
            USING (
                collection_id = NULLIF(
                    current_setting('anila.collection_id', true),
                    ''
                )::int
            )
        """
    )

    # ── 3. SECURITY DEFINER resolver for the by-PK blob path ────────────────
    # Returns ONLY the collection_id for an image id, bypassing RLS (runs as the
    # migration superuser owner). Lets the blob endpoint learn which collection
    # an image belongs to so it can enforce ownership + set the GUC, without a
    # cross-service API contract change. search_path pinned to defeat injection.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ingestion_image_collection_id(p_id bigint)
        RETURNS integer
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $func$
            SELECT collection_id FROM ingestion_images WHERE id = p_id
        $func$
        """
    )
    # Lock execution down to the app role (if present); never world-callable.
    op.execute(
        "REVOKE ALL ON FUNCTION ingestion_image_collection_id(bigint) FROM PUBLIC"
    )
    op.execute(
        """
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'csp_app') THEN
                GRANT EXECUTE ON FUNCTION ingestion_image_collection_id(bigint)
                    TO csp_app;
            END IF;
        END
        $do$
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS ingestion_image_collection_id(bigint)")
    op.execute("DROP POLICY IF EXISTS images_collection_isolation ON ingestion_images")
    op.execute("ALTER TABLE ingestion_images NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE ingestion_images DISABLE ROW LEVEL SECURITY")
