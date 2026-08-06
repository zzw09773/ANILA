"""#56 — alembic ``r1_0032`` on real PostgreSQL: the data fix itself.

What this file has to prove that a SQLite test cannot:

  * the rewrite is driven by ``UPDATE ... FROM (SELECT ... GROUP BY
    lower(name) HAVING count(DISTINCT name) = 1)``, which SQLite will
    not run;
  * chunk provenance lives behind ``document_chunks``' and
    ``ingestion_images``' FORCE row-level security, so a rewrite that
    forgets the ``anila.collection_id`` GUC updates **zero rows and
    reports success** when the migration role is not a superuser — the
    same shape as FAKE-CONTROLS #52.

⚠ **The migration is deliberately run by a NOSUPERUSER / NOBYPASSRLS
role that owns the tables**, not by the DSN's superuser. This is the
whole reason the file can see anything: superusers bypass RLS outright,
so under a superuser DSN every RLS-dependent mutation is invisible by
construction — deleting the migration's ``set_config`` call would leave
a suite of this shape entirely green. The fixture creates that role,
hands it ownership of ``public``, and runs ``alembic upgrade r1_0032``
as it.

Every table the migration claims to fix is seeded in all five
categories (recase / already-correct / unregistered / ambiguous /
deactivated-but-sole-candidate), so dropping any one table from the
migration's list turns a test red, and so does either half of the
candidate rule — adding ``AND is_active`` or removing the
``model_type = 'embedding'`` filter.

Never touches the live ``csp`` database: the fixture creates a
uuid-suffixed scratch database, migrates it, and drops it.

Skipped unless ``ANILA_TEST_PG_DSN`` is set (superuser, so it can
CREATE DATABASE and CREATE ROLE) — same contract as
``test_platform_embedding_pg.py``.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT  # noqa: E402

_DSN = os.environ.get("ANILA_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="ANILA_TEST_PG_DSN not set — needs PostgreSQL + CREATE DATABASE",
)

_CSP_ROOT = Path(__file__).resolve().parents[1]
_ALEMBIC_INI = _CSP_ROOT / "alembic.ini"

# The live pair, recorded in FAKE-CONTROLS #56 — not invented here.
REGISTERED = "nvidia/nv-embed-v2"
MISCASED = "nvidia/NV-embed-V2"
# A **deactivated chat** model that collides case-insensitively with the
# real embedder. Counting it as a candidate would make the embedder look
# ambiguous and silently skip the rows this migration exists to fix.
DECOY_CHAT = "NVIDIA/NV-Embed-V2"
# No registration at all. Nothing may guess a spelling for it.
UNREGISTERED = "legacy/old-embedder"
# Two ACTIVE embedding registrations differing only by case — genuine
# ambiguity (#56 item 8). Rows naming it must be left alone.
AMBIG_A = "acme/Emb-1"
AMBIG_B = "acme/emb-1"
AMBIG_REQUEST = "ACME/EMB-1"
# A **deactivated embedding** registration that is the only candidate for
# its case-folded name. It MUST still be recased: a deactivated embedder
# is the correct name for the vectors it already produced, and #56 item 9
# plus the 409 message tell the operator to re-designate — and if
# necessary reactivate — exactly that model. Filtering ``is_active`` out
# of the candidate set would strand that corpus at the moment the
# operator is trying to recover it. This category is what makes that
# decision a test instead of three comment blocks.
DEACTIVATED_REG = "retired/emb-9"
DEACTIVATED_SEED = "RETIRED/Emb-9"

# category -> (value seeded into every column, value expected afterwards)
CATEGORIES = {
    "miscased": (MISCASED, REGISTERED),
    "correct": (REGISTERED, REGISTERED),
    "unregistered": (UNREGISTERED, UNREGISTERED),
    "ambiguous": (AMBIG_REQUEST, AMBIG_REQUEST),
    "deactivated": (DEACTIVATED_SEED, DEACTIVATED_REG),
}

# 4000-d halfvec literal for the two NOT NULL embedding columns.
_VEC = "('[' || array_to_string(array_fill(0.1::real, ARRAY[4000]), ',') || ']')::halfvec"


def _load_migration_module():
    """Import the revision file by path — ``migrations/`` is not a package."""
    import importlib.util

    path = (
        _CSP_ROOT
        / "migrations"
        / "versions"
        / "r1_0032_canonical_embedding_model_name.py"
    )
    spec = importlib.util.spec_from_file_location("_r1_0032_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scratch_url(admin_dsn: str, dbname: str) -> str:
    parts = urlparse(admin_dsn)
    return urlunparse(parts._replace(path=f"/{dbname}"))


def _role_url(db_dsn: str, role: str, password: str) -> str:
    parts = urlparse(db_dsn)
    host = parts.hostname or "127.0.0.1"
    port = f":{parts.port}" if parts.port else ""
    return urlunparse(parts._replace(netloc=f"{role}:{password}@{host}{port}"))


def _dbname_of(dsn: str) -> str:
    return (urlparse(dsn).path or "").lstrip("/") or "postgres"


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(f"{record.levelname} {record.getMessage()}")


def _alembic(target: str, dsn: str, *, downgrade: bool = False) -> list[str]:
    """Run alembic against ``dsn``; return what the migration logged.

    ``migrations/env.py:57`` calls ``logging.config.fileConfig``, which
    tears down handlers attached before the run — so a capture handler
    added here would be silently discarded and this function would
    always return an empty list, i.e. it would "prove" the operator
    output exists by never looking at it. Neutralising ``fileConfig``
    for the duration is what makes the capture real; it is restored in
    ``finally``.
    """
    import logging.config

    from alembic import command
    from alembic.config import Config

    logger = logging.getLogger("alembic.runtime.migration")
    cap = _Capture()
    prev_level = logger.level
    prev_file_config = logging.config.fileConfig
    logging.config.fileConfig = lambda *a, **k: None
    logger.addHandler(cap)
    logger.setLevel(logging.INFO)

    prev_mig = os.environ.get("MIGRATION_DATABASE_URL")
    prev_db = os.environ.get("DATABASE_URL")
    os.environ["MIGRATION_DATABASE_URL"] = dsn
    os.environ["DATABASE_URL"] = dsn
    old_cwd = os.getcwd()
    os.chdir(_CSP_ROOT)
    try:
        cfg = Config(str(_ALEMBIC_INI))
        if downgrade:
            command.downgrade(cfg, target)
        else:
            command.upgrade(cfg, target)
    finally:
        os.chdir(old_cwd)
        logging.config.fileConfig = prev_file_config
        logger.removeHandler(cap)
        logger.setLevel(prev_level)
        for key, prev in (
            ("MIGRATION_DATABASE_URL", prev_mig),
            ("DATABASE_URL", prev_db),
        ):
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
    return cap.lines


@pytest.fixture(scope="module")
def scratch_dsn():
    assert _DSN
    admin_db = _dbname_of(_DSN)
    scratch = f"r1_0032_{uuid.uuid4().hex[:10]}"
    assert scratch != admin_db, "refusing to migrate the DSN's own database"
    assert scratch != "csp", "refusing to touch the live platform database"

    admin = psycopg2.connect(_DSN)
    admin.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = admin.cursor()
    try:
        cur.execute(f'CREATE DATABASE "{scratch}"')
    finally:
        cur.close()
        admin.close()

    url = _scratch_url(_DSN, scratch)
    try:
        yield url
    finally:
        admin = psycopg2.connect(_DSN)
        admin.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        cur = admin.cursor()
        try:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (scratch,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{scratch}"')
        finally:
            cur.close()
            admin.close()


def _column_default(cur) -> str | None:
    cur.execute(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name = 'ingestion_collections' "
        "  AND column_name = 'embedding_model'"
    )
    return cur.fetchone()[0]


@pytest.fixture(scope="module")
def seeded(scratch_dsn):
    """Migrate to the revision *before* the fix, plant the defect in all
    four tables, then hand the tables to a non-superuser owner."""
    _alembic("r1_0031", scratch_dsn)

    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute(
        "INSERT INTO users (username, hashed_password, role, is_approved) "
        "VALUES (%s, 'x', 'developer', true) RETURNING id",
        (f"r1_0032_owner_{uuid.uuid4().hex[:8]}",),
    )
    owner = cur.fetchone()[0]

    for name, mtype, active in (
        (REGISTERED, "embedding", True),
        (DECOY_CHAT, "llm", False),
        (AMBIG_A, "embedding", True),
        (AMBIG_B, "embedding", True),
        (DEACTIVATED_REG, "embedding", False),
    ):
        cur.execute(
            "INSERT INTO model_registry (name, display_name, model_type, "
            "endpoint_url, is_active) VALUES (%s, %s, %s, "
            "'http://embed.test/v1', %s)",
            (name, name, mtype, active),
        )

    cfg = json.dumps({"strategy": "fixed"})
    ids: dict[str, int] = {}
    for key, (stored, _expected) in CATEGORIES.items():
        cur.execute(
            "INSERT INTO ingestion_collections "
            "  (name, chunking_config, embedding_model, embedding_dim, created_by) "
            "VALUES (%s, %s::jsonb, %s, 4000, %s) RETURNING id",
            (f"kb-{key}", cfg, stored, owner),
        )
        cid = cur.fetchone()[0]
        ids[key] = cid

        cur.execute(
            "INSERT INTO ingestion_documents "
            "  (collection_id, filename, sha256, mime_type, status) "
            "VALUES (%s, %s, %s, 'application/pdf', 'indexed') RETURNING id",
            (cid, f"{key}.pdf", uuid.uuid4().hex * 2),
        )
        doc = cur.fetchone()[0]

        # Chunk / image provenance mirrors the collection column, because
        # that is literally what migration r1_0018 did to the live corpus
        # (``SET embedding_source_model = ic.embedding_model``).
        # FORCE RLS applies to the owner too, so set the GUC to insert.
        cur.execute("SELECT set_config('anila.collection_id', %s, false)", (str(cid),))
        cur.execute(
            "INSERT INTO document_chunks "
            "  (collection_id, document_id, chunk_key, content, "
            "   embedding_source_model) "
            "VALUES (%s, %s, %s, '第三條 承辦單位應於七日內完成審查。', %s)",
            (cid, doc, f"chunk:{key}:1", stored),
        )
        cur.execute(
            "INSERT INTO ingestion_images "
            "  (collection_id, document_id, image_id, storage_path, "
            "   embedding_source_model) "
            "VALUES (%s, %s, %s, %s, %s)",
            (cid, doc, f"img:{key}:1", f"/blobs/{key}.png", stored),
        )

        cur.execute(
            "INSERT INTO conversations (user_id, title) VALUES (%s, %s) RETURNING id",
            (owner, f"conv-{key}"),
        )
        conv = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO conversation_memory_chunks "
            "  (user_id, conversation_id, role, content, embedding, "
            "   embedding_source_model) "
            f"VALUES (%s, %s, 'user', 'x', {_VEC}, %s)",
            (owner, conv, stored),
        )
    cur.execute("SELECT set_config('anila.collection_id', '', false)")

    # ── hand ownership to a role that does NOT bypass RLS ──────────────
    role = f"r1_0032_mig_{uuid.uuid4().hex[:8]}"
    password = uuid.uuid4().hex
    cur.execute(
        f"CREATE ROLE {role} LOGIN PASSWORD %s NOSUPERUSER NOBYPASSRLS", (password,)
    )
    cur.execute(
        f"""
        DO $do$
        DECLARE r record;
        BEGIN
          FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public'
          LOOP
            EXECUTE format('ALTER TABLE public.%I OWNER TO {role}', r.tablename);
          END LOOP;
        END
        $do$;
        """
    )
    mig_dsn = _role_url(scratch_dsn, role, password)

    yield conn, cur, ids, mig_dsn, role

    cur.close()
    conn.close()


def _collection_models(cur, ids: dict[str, int]) -> dict[str, str]:
    out = {}
    for key, cid in ids.items():
        cur.execute(
            "SELECT embedding_model FROM ingestion_collections WHERE id = %s", (cid,)
        )
        row = cur.fetchone()
        assert row, f"collection row for {key} vanished"
        out[key] = row[0]
    return out


def _memory_models(cur, ids: dict[str, int]) -> dict[str, str]:
    out = {}
    for key in ids:
        cur.execute(
            "SELECT m.embedding_source_model FROM conversation_memory_chunks m "
            "JOIN conversations c ON c.id = m.conversation_id "
            "WHERE c.title = %s",
            (f"conv-{key}",),
        )
        row = cur.fetchone()
        assert row, f"conversation_memory_chunks row for {key} vanished"
        out[key] = row[0]
    return out


def _scoped_models(cur, table: str, ids: dict[str, int]) -> dict[str, str]:
    """Reads under the RLS GUC — that is the only way to see the rows."""
    out = {}
    for key, cid in ids.items():
        cur.execute("SELECT set_config('anila.collection_id', %s, false)", (str(cid),))
        cur.execute(
            f"SELECT embedding_source_model FROM {table} WHERE collection_id = %s",
            (cid,),
        )
        rows = cur.fetchall()
        assert rows, f"{table} row for {key} vanished"
        out[key] = rows[0][0]
    cur.execute("SELECT set_config('anila.collection_id', '', false)")
    return out


def _all_four(cur, ids: dict[str, int]) -> dict[str, dict[str, str]]:
    return {
        "ingestion_collections": _collection_models(cur, ids),
        "conversation_memory_chunks": _memory_models(cur, ids),
        "document_chunks": _scoped_models(cur, "document_chunks", ids),
        "ingestion_images": _scoped_models(cur, "ingestion_images", ids),
    }


@pytest.fixture(scope="module")
def upgraded(seeded):
    """Run ``alembic upgrade r1_0032`` **as the non-superuser owner**.

    Returns the fixture tuple plus everything the migration logged, so
    the "what did it leave alone" assertions read the real output rather
    than a re-derived guess.
    """
    conn, cur, ids, mig_dsn, role = seeded
    assert _column_default(cur) is not None, "0014's default should still be here"
    log = _alembic("r1_0032", mig_dsn)
    return conn, cur, ids, mig_dsn, role, log


# ── the hazard, measured before the fix runs ─────────────────────────────────


def test_the_migration_role_really_does_not_bypass_rls(seeded):
    """If this fails, nothing else in this file is testing what it claims.

    ``document_chunks`` / ``ingestion_images`` are ENABLE + FORCE RLS
    keyed on ``anila.collection_id`` (0014 / 0019 / 0037). FORCE means
    the table owner is not exempt either. The migration role owns these
    tables and does not bypass RLS, so without the GUC it sees an empty
    table — and a migration that updated nothing would still exit 0.
    """
    conn, cur, ids, mig_dsn, _role = seeded
    mig = psycopg2.connect(mig_dsn)
    mig.autocommit = True
    mcur = mig.cursor()
    try:
        mcur.execute("SELECT current_setting('is_superuser')")
        assert mcur.fetchone()[0] == "off"
        for table in ("document_chunks", "ingestion_images"):
            mcur.execute(f"SELECT count(*) FROM {table}")
            assert mcur.fetchone()[0] == 0, f"{table}: RLS is not in force"
            mcur.execute(
                "SELECT set_config('anila.collection_id', %s, false)",
                (str(ids["miscased"]),),
            )
            mcur.execute(f"SELECT count(*) FROM {table}")
            assert mcur.fetchone()[0] == 1
            mcur.execute("SELECT set_config('anila.collection_id', '', false)")
    finally:
        mcur.close()
        mig.close()


# ── the fix ──────────────────────────────────────────────────────────────────


def test_r1_0032_recases_only_what_the_registry_can_vouch_for(upgraded):
    """Upgrade as the non-superuser owner, then read every row back.

    Five categories × the four tables the migration claims to fix.
    Mutants this kills:
      * deleting any one of the four (table, column) entries — that
        table's ``miscased`` row stays wrong;
      * deleting the per-collection ``set_config`` call — both
        RLS-scoped tables stop moving, because the migration role does
        not bypass RLS here;
      * counting non-embedding registrations as candidates — the
        deactivated ``llm`` row named ``NVIDIA/NV-Embed-V2`` would make
        the real embedder ambiguous and the ``miscased`` rows would stay;
      * **adding ``AND is_active`` to the candidate set** — the
        ``deactivated`` category's sole candidate would drop out and
        ``RETIRED/Emb-9`` would keep its capitals, stranding a corpus at
        exactly the moment the operator is trying to recover it;
      * recasing rows the registry cannot vouch for — ``unregistered``
        and ``ambiguous`` would move;
      * leaving 0014's column default in place.
    """
    _conn, cur, ids, _mig_dsn, _role, _log = upgraded

    assert _column_default(cur) is None

    tables = _all_four(cur, ids)
    for table, values in tables.items():
        for key, (_seed, expected) in CATEGORIES.items():
            assert values[key] == expected, (
                f"{table}[{key}] = {values[key]!r}, expected {expected!r}"
            )


def test_the_migration_reports_what_it_left_alone(upgraded):
    """A data fix that only logs what it changed teaches the operator
    that silence means "all clean" — the exact habit #56 exists to break.

    Every table carries exactly one ambiguous row and one unregistered
    row, so every table must produce a WARNING naming both counts.

    Mutants:
      * delete the ``_report_left`` calls (or the ``_left_sql`` query)
        and the operator sees four cheerful "N row(s) recased" lines and
        nothing about the two rows per table still carrying a name
        retrieval can only find case-insensitively;
      * add ``AND is_active`` to the candidate set and the
        ``deactivated`` row stops being recognised as registered at all
        — ``unregistered`` goes 1 → 2. The exact counts are asserted, so
        this observability property is pinned rather than merely
        available: the log would have told an operator, and now it also
        tells the suite.
    """
    *_rest, log = upgraded
    text = "\n".join(log)

    for table in (
        "ingestion_collections",
        "conversation_memory_chunks",
        "document_chunks",
        "ingestion_images",
    ):
        left = [
            line
            for line in log
            if line.startswith("WARNING") and f"{table} — LEFT UNFIXED" in line
        ]
        assert left, f"{table}: nothing said about the rows left alone\n{text}"
        assert "1 row(s) whose model name the registry spells more than one way" in left[0]
        assert "1 row(s) naming no registered embedding model at all" in left[0]


def test_rerunning_the_rewrite_touches_nothing(upgraded):
    """Alembic will not re-run the revision, but an operator re-running
    the statement by hand (or a re-stamped database) must be safe."""
    _conn, cur, ids, _mig_dsn, _role, _log = upgraded
    _rewrite_sql = _load_migration_module()._rewrite_sql

    for table, column in (
        ("ingestion_collections", "embedding_model"),
        ("conversation_memory_chunks", "embedding_source_model"),
    ):
        cur.execute(_rewrite_sql(table, column, scoped=False))
        assert cur.rowcount == 0, table

    for table in ("document_chunks", "ingestion_images"):
        for cid in ids.values():
            cur.execute(
                "SELECT set_config('anila.collection_id', %s, false)", (str(cid),)
            )
            cur.execute(
                _rewrite_sql(table, "embedding_source_model", scoped=True).replace(
                    ":cid", str(cid)
                )
            )
            assert cur.rowcount == 0, f"{table}/{cid}"
    cur.execute("SELECT set_config('anila.collection_id', '', false)")


def test_downgrade_restores_the_old_default_and_says_the_data_is_one_way(upgraded):
    """Reversibility, stated honestly: schema back, casing not."""
    _conn, cur, ids, mig_dsn, _role, _log = upgraded

    _alembic("r1_0031", mig_dsn, downgrade=True)

    default = _column_default(cur)
    assert default is not None and MISCASED in default

    # The rows stay canonical — documented as irreversible, and this is
    # the assertion that keeps that documentation true.
    for table, values in _all_four(cur, ids).items():
        assert values["miscased"] == REGISTERED, table

    _alembic("r1_0032", mig_dsn)
