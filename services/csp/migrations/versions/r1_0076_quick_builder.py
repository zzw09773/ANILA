"""助手快製、不可變流程版本與 MCP 治理。

Revision ID: r1_0076
Revises: r1_0075
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from alembic import op

revision = "r1_0076"
down_revision = "r1_0075"
branch_labels = None
depends_on = None
J = sa.JSON().with_variant(JSONB, "postgresql")


def fk(name, table, nullable=True, ondelete=None):
    return sa.Column(
        name,
        sa.Integer(),
        sa.ForeignKey(table + ".id", ondelete=ondelete),
        nullable=nullable,
    )


def c(name, kind, nullable=False, **kw):
    return sa.Column(name, kind, nullable=nullable, **kw)


def upgrade():
    op.create_table(
        "quick_assistants",
        c("id", sa.Integer(), primary_key=True),
        fk("owner_user_id", "users", False),
        fk("department_id", "departments", False),
        c("draft_revision", sa.Integer()),
        c("pending_revision", sa.Integer(), True),
        c("published_revision", sa.Integer(), True),
        c("disabled", sa.Boolean()),
        c("disabled_revision", sa.Integer(), True),
        c("created_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "quick_versions",
        c("id", sa.Integer(), primary_key=True),
        fk("assistant_id", "quick_assistants", False, "CASCADE"),
        c("revision", sa.Integer()),
        c("payload", J),
        c("payload_hash", sa.String(64)),
        c("status", sa.String(24)),
        c("review_reason", sa.Text(), True),
        fk("reviewed_by", "users"),
        c("reviewed_at", sa.DateTime(timezone=True), True),
        fk("created_by", "users", False),
        c("created_at", sa.DateTime(timezone=True)),
        fk("agent_id", "agents", ondelete="SET NULL"),
        sa.UniqueConstraint("agent_id", name="uq_quick_versions_agent_id"),
        sa.UniqueConstraint(
            "assistant_id", "revision", name="uq_quick_version_revision"
        ),
    )
    op.create_table(
        "quick_runs",
        c("id", sa.Integer(), primary_key=True),
        fk("version_id", "quick_versions", False),
        fk("user_id", "users", False),
        c("status", sa.String(24)),
        c("result", J),
        c("created_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "quick_templates",
        c("id", sa.Integer(), primary_key=True),
        c("name", sa.String(100)),
        c("description", sa.Text()),
        c("graph", J),
        fk("updated_by", "users", False),
        c("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "quick_collection_publications",
        sa.Column(
            "collection_id",
            sa.Integer(),
            sa.ForeignKey("ingestion_collections.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        c("scope", sa.String(20)),
        c("target_department_ids", J),
        fk("approved_by", "users", False),
        c("approved_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "quick_mcp_services",
        c("id", sa.Integer(), primary_key=True),
        c("name", sa.String(120)),
        c("description", sa.Text()),
        c("governance_revision", sa.Integer(), server_default="0"),
        c("endpoint_url", sa.String(2048), unique=True),
        fk("department_id", "departments", ondelete="RESTRICT"),
        c("is_public", sa.Boolean(), server_default="false"),
        c("status", sa.String(16), server_default="pending"),
        fk("registered_by_user_id", "users", False, "RESTRICT"),
        fk("approved_by_user_id", "users", ondelete="SET NULL"),
        c("approved_at", sa.DateTime(timezone=True), True),
        c("created_at", sa.DateTime(timezone=True)),
        c("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "quick_mcp_tools",
        c("id", sa.Integer(), primary_key=True),
        fk("service_id", "quick_mcp_services", False, "CASCADE"),
        c("name", sa.String(128)),
        c("description", sa.Text()),
        c("input_schema", sa.JSON()),
        c("annotations", sa.JSON()),
        c("schema_fingerprint", sa.String(64)),
        c("approved_fingerprint", sa.String(64), True),
        c("is_read_only", sa.Boolean(), server_default="false"),
        c("is_approved", sa.Boolean(), server_default="false"),
        c("created_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "service_id", "name", name="uq_quick_mcp_tool_service_name"
        ),
    )
    for table, cols in (
        ("quick_assistants", ("owner_user_id", "department_id")),
        ("quick_versions", ("assistant_id",)),
        ("quick_runs", ("version_id", "user_id")),
        ("quick_mcp_services", ("department_id",)),
        ("quick_mcp_tools", ("service_id",)),
    ):
        for col in cols:
            op.create_index("ix_" + table + "_" + col, table, [col])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""CREATE FUNCTION quick_version_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.payload IS DISTINCT FROM OLD.payload OR NEW.payload_hash IS DISTINCT FROM OLD.payload_hash
             OR NEW.revision IS DISTINCT FROM OLD.revision OR NEW.assistant_id IS DISTINCT FROM OLD.assistant_id
             OR NEW.created_by IS DISTINCT FROM OLD.created_by THEN RAISE EXCEPTION 'IMMUTABLE_VERSION'; END IF;
          RETURN NEW;
        END $$""")
        op.execute(
            "CREATE TRIGGER quick_version_immutable BEFORE UPDATE ON quick_versions FOR EACH ROW EXECUTE FUNCTION quick_version_immutable()"
        )
    elif op.get_bind().dialect.name == "sqlite":
        op.execute("""CREATE TRIGGER quick_version_immutable BEFORE UPDATE ON quick_versions
        WHEN NEW.payload != OLD.payload OR NEW.payload_hash != OLD.payload_hash OR NEW.revision != OLD.revision
          OR NEW.assistant_id != OLD.assistant_id OR NEW.created_by != OLD.created_by
        BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VERSION'); END""")


def downgrade():
    agents = sa.table("agents", sa.column("id"), sa.column("approval_status"))
    versions = sa.table("quick_versions", sa.column("agent_id"))
    op.execute(
        agents.update()
        .where(agents.c.id.in_(sa.select(versions.c.agent_id)))
        .values(approval_status="disabled")
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER quick_version_immutable ON quick_versions")
        op.execute("DROP FUNCTION quick_version_immutable()")
    elif op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TRIGGER quick_version_immutable")
    for table in (
        "quick_mcp_tools",
        "quick_mcp_services",
        "quick_collection_publications",
        "quick_templates",
        "quick_runs",
        "quick_versions",
        "quick_assistants",
    ):
        op.drop_table(table)
