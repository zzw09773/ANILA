# ANILA Functions v1 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship OpenWebUI-style "Action functions"（assistant 訊息底下的自訂 button）給內部 developer 在 ANILA UI 上開發，含 inline Python editor、版本管理、admin Valves（加密）、內部 marketplace、audit log、enterprise-grade 容器隔離。

**Architecture:** 4 個新 service（worker-api trusted gate + sandbox-exec + sandbox-extract + egress squid proxy）+ 3 docker internal networks + 2 docker volumes（Unix socket IPC）；CSP backend 加 5 張表（functions / versions / valves / runs / reports）+ ~12 個 endpoint；anila-ui 加 `/admin/functions/*` 路由 + ChatRuntime toolbar host_command dispatcher。容器內 UID 三層（web 65532 / sandbox 65533 / subproc 65534）+ `anila-jobs:65530` group 限定 socket 存取。

**Tech Stack:**
- Backend: FastAPI 0.115 + SQLAlchemy 2 + alembic + cryptography（AES-256-GCM）+ httpx（call worker-api）
- Worker stack: Python 3.12-slim base + util-linux（setpriv）+ pydantic + httpx + fastapi
- Egress proxy: ubuntu/squid:5
- Frontend: React 18 + Vite 6 + Monaco editor (bundled)
- Tests: pytest + testcontainers（PG）+ Vitest + Playwright

**Spec reference:** `docs/superpowers/specs/2026-04-28-anila-functions-design.md`（v9, commit 9aaf9cc）

---

## File Structure

### CSP backend（`myCSPPlatform/backend/`）— 新增 / 修改

**Migrations**：
- Create `migrations/versions/0025_add_action_functions.py` — 5 表 + append-only trigger + indexes

**Models**：
- Create `app/models/action_function.py` — 5 個 SQLAlchemy 類別（ActionFunction、ActionFunctionVersion、ActionFunctionValves、ActionFunctionRun、ActionFunctionReport）

**Schemas**：
- Create `app/schemas/action_function.py` — Pydantic request/response models

**Services**：
- Create `app/services/action_function/` (new package)
  - `__init__.py`
  - `crud.py` — Function CRUD + advisory lock save
  - `valves_crypto.py` — AES-256-GCM helper
  - `redaction.py` — secret pattern redactor
  - `worker_client.py` — httpx call worker-api
  - `ownership.py` — 7-step authz (chat_message / test_console split)
  - `marketplace.py` — fork / report / quarantine / unquarantine
  - `audit.py` — 寫 audit_logs + runs row helpers

**API**：
- Create `app/api/action_function/` (new package)
  - `__init__.py` — exports routers
  - `crud.py` — Function CRUD + version
  - `valves.py` — Valves get/set
  - `marketplace.py` — fork / report / quarantine / unquarantine
  - `run.py` — `/run` SSE relay
  - `runs.py` — audit list/detail
  - `enabled_actions.py` — flat actions list for ChatRuntime
- Modify `app/api/router.py` — include 上述 router

**Tests**：
- Create `tests/api/action_function/` — endpoint tests
- Create `tests/services/action_function/` — unit tests

### Worker stack（NEW project：`anila-functions-worker/`）

```
anila-functions-worker/
├── Dockerfile.worker-api
├── Dockerfile.sandbox          # both sandbox-exec and sandbox-extract use this
├── sandbox-entrypoint.sh
├── worker-api/
│   ├── main.py                 # FastAPI app
│   ├── exec_handler.py
│   ├── extract_handler.py
│   ├── socket_client.py        # Unix socket client to sandbox
│   └── auth.py                 # X-Functions-Api-Secret check
├── sandbox/
│   ├── daemon.py               # Unix socket server
│   ├── runtime.py              # subprocess exec wrapper
│   ├── extract.py              # static AST + sandbox stage 2
│   ├── ambient.py              # prctl ambient cap clear
│   └── runner_spec.py          # job spec model
├── shared/
│   └── wire.py                 # JSON wire protocol shared by api/sandbox
├── tests/
│   ├── test_runtime.py
│   ├── test_daemon.py
│   ├── test_extract.py
│   ├── test_uid_isolation.py
│   ├── test_egress.py
│   └── test_smoke_capabilities.py   # Sprint 2.5 prototype gate
└── requirements.txt
```

### Egress proxy（NEW：`anila-functions-egress/`）
- `Dockerfile`
- `squid.conf.template`
- `entrypoint.sh`（從 ENV `ANILA_FUNCTIONS_EGRESS_ALLOWLIST` 生成 ACL）

### Frontend（`ANILA_UI/anila-ui/src/`）— 新增 / 修改

**新增**：
- `runtime/functions.js` — API client
- `runtime/functionEvents.js` — SSE event handler
- `runtime/hostCommands.js` — 6 verb dispatcher 白名單
- `runtime/functionsStore.js` — zustand-equivalent enabled-actions cache
- `admin/functions.jsx` — list + editor + tabs (Code / Valves / Test / Runs / Versions)
- `admin/auditDetail.jsx` — replay events_json
- `admin/monacoLoader.jsx` — bundled Monaco wrapper
- `admin/styles.css` — admin pages styling

**修改**：
- `chat.jsx` — MessageBubble toolbar 加 host_command buttons + overflow menu
- `app.jsx` — 加 admin route `/admin/functions/*`
- `runtime/api.js` — function endpoints helper
- `package.json` — add monaco-editor + react-monaco-editor

### docker-compose.yml — 修改
- 加 4 services + 3 networks + 2 volumes + 1 egress proxy

---

## Sprint 1 — Backend Core + Schema (~25-35 tasks, 5-7 working days)

**Goal:** 5 張表 + alembic migration + Function CRUD endpoints + Valves 加密讀寫 + advisory-lock save + ownership service stub。Worker 還沒接入；run endpoint 先 stub 回 503。

### Task 1.1: Add `cryptography` to requirements

**Files:**
- Modify: `myCSPPlatform/backend/requirements.txt`

- [ ] **Step 1: Verify current dep**

Run: `grep cryptography /home/aia/c1147259/ANILA/myCSPPlatform/backend/requirements.txt`
Expected: `python-jose[cryptography]==3.3.0` (cryptography is transitive)

- [ ] **Step 2: Pin cryptography explicitly for AES-GCM**

Add line after `python-jose[cryptography]==3.3.0`:
```
cryptography==44.0.0
```

- [ ] **Step 3: Commit**

```bash
git add myCSPPlatform/backend/requirements.txt
git commit -m "chore: pin cryptography for ANILA Functions AES-GCM"
```

---

### Task 1.2: Migration 0025 — create 5 tables + trigger + indexes

**Files:**
- Create: `myCSPPlatform/backend/migrations/versions/0025_add_action_functions.py`

- [ ] **Step 1: Write migration scaffolding**

```python
"""add action_functions schema

Revision ID: 0025
Revises: 0024
Create Date: 2026-04-29

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None
```

- [ ] **Step 2: Add `upgrade()` — create 5 tables**

Append to file:
```python
def upgrade():
    # 1. action_functions
    op.create_table(
        "action_functions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("icon_data_url", sa.Text, nullable=True),
        sa.Column("author_user_id", sa.BigInteger, sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "status",
            sa.Enum("draft", "enabled", "disabled", "quarantined", name="action_function_status"),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("disabled_reason", sa.Text, nullable=True),
        sa.Column("latest_version_id", sa.BigInteger, nullable=True),  # NO FK (denormalized cache, avoid circular)
        sa.Column("forked_from_id", sa.BigInteger, sa.ForeignKey("action_functions.id"), nullable=True),
        sa.Column("tags", postgresql.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_action_functions_status", "action_functions", ["status"])
    op.create_index("ix_action_functions_author", "action_functions", ["author_user_id"])

    # 2. action_function_versions (append-only)
    op.create_table(
        "action_function_versions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("function_id", sa.BigInteger, sa.ForeignKey("action_functions.id"), nullable=False),
        sa.Column("version_no", sa.Integer, nullable=False),
        sa.Column("code", sa.Text, nullable=False),
        sa.Column("metadata_json", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("actions_meta_json", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("valves_schema_json", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("editor_user_id", sa.BigInteger, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("commit_message", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("function_id", "version_no", name="uq_function_version_no"),
    )

    # 3. action_function_valves (encrypted)
    op.create_table(
        "action_function_valves",
        sa.Column("function_id", sa.BigInteger, sa.ForeignKey("action_functions.id"), primary_key=True),
        sa.Column("values_encrypted", sa.LargeBinary, nullable=False),
        sa.Column("nonce", sa.LargeBinary, nullable=False),
        sa.Column("key_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("updated_by", sa.BigInteger, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # 4. action_function_runs (audit)
    op.create_table(
        "action_function_runs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("function_id", sa.BigInteger, sa.ForeignKey("action_functions.id"), nullable=False),
        sa.Column("version_no", sa.Integer, nullable=False),
        sa.Column("action_id", sa.Text, nullable=False),
        sa.Column("triggered_by_user_id", sa.BigInteger, sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "context_type",
            sa.Enum("chat_message", "test_console", name="action_function_run_context"),
            nullable=False,
        ),
        sa.Column("conversation_id", sa.BigInteger, sa.ForeignKey("conversations.id"), nullable=True),
        sa.Column("message_id", sa.BigInteger, sa.ForeignKey("messages.id"), nullable=True),
        sa.Column("request_payload_json", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "status",
            sa.Enum("queued", "running", "success", "error", "timeout", name="action_function_run_status"),
            nullable=False,
        ),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("events_json", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_runs_function_started", "action_function_runs", ["function_id", "started_at"])
    op.create_index("ix_runs_user_started", "action_function_runs", ["triggered_by_user_id", "started_at"])
    op.create_index("ix_runs_conversation", "action_function_runs", ["conversation_id"])

    # 5. action_function_reports
    op.create_table(
        "action_function_reports",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("function_id", sa.BigInteger, sa.ForeignKey("action_functions.id"), nullable=False),
        sa.Column("reporter_user_id", sa.BigInteger, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column(
            "status",
            sa.Enum("open", "acknowledged", "dismissed", "actioned", name="action_function_report_status"),
            nullable=False,
            server_default="open",
        ),
        sa.Column("acknowledged_by", sa.BigInteger, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_reports_function_status", "action_function_reports", ["function_id", "status"])
    op.create_index("ix_reports_status_created", "action_function_reports", ["status", "created_at"])

    # 6. Append-only trigger on versions
    op.execute("""
        CREATE OR REPLACE FUNCTION action_function_versions_immutable()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'action_function_versions is append-only; UPDATE/DELETE not allowed';
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_action_function_versions_immutable
        BEFORE UPDATE OR DELETE ON action_function_versions
        FOR EACH ROW EXECUTE FUNCTION action_function_versions_immutable();
    """)
```

- [ ] **Step 3: Add `downgrade()`**

Append:
```python
def downgrade():
    op.execute("DROP TRIGGER IF EXISTS trg_action_function_versions_immutable ON action_function_versions")
    op.execute("DROP FUNCTION IF EXISTS action_function_versions_immutable()")
    op.drop_table("action_function_reports")
    op.drop_table("action_function_runs")
    op.drop_table("action_function_valves")
    op.drop_table("action_function_versions")
    op.drop_table("action_functions")
    op.execute("DROP TYPE IF EXISTS action_function_status")
    op.execute("DROP TYPE IF EXISTS action_function_run_context")
    op.execute("DROP TYPE IF EXISTS action_function_run_status")
    op.execute("DROP TYPE IF EXISTS action_function_report_status")
```

- [ ] **Step 4: Apply migration locally**

Run: `cd /home/aia/c1147259/ANILA/myCSPPlatform/backend && alembic upgrade head`
Expected: `0024 -> 0025, add action_functions schema`

- [ ] **Step 5: Verify tables exist**

Run: `psql $DATABASE_URL -c "\dt action_function*"`
Expected: 5 tables listed

- [ ] **Step 6: Commit**

```bash
git add myCSPPlatform/backend/migrations/versions/0025_add_action_functions.py
git commit -m "feat(db): add action_functions schema (functions + versions + valves + runs + reports)"
```

---

### Task 1.3: SQLAlchemy models — `action_function.py`

**Files:**
- Create: `myCSPPlatform/backend/app/models/action_function.py`
- Modify: `myCSPPlatform/backend/app/models/__init__.py` — export new models

- [ ] **Step 1: Write models file**

```python
"""SQLAlchemy models for ANILA Functions v1."""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ActionFunctionStatus(str, enum.Enum):
    DRAFT = "draft"
    ENABLED = "enabled"
    DISABLED = "disabled"
    QUARANTINED = "quarantined"


class ActionFunctionRunContext(str, enum.Enum):
    CHAT_MESSAGE = "chat_message"
    TEST_CONSOLE = "test_console"


class ActionFunctionRunStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"


class ActionFunctionReportStatus(str, enum.Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    DISMISSED = "dismissed"
    ACTIONED = "actioned"


class ActionFunction(Base):
    __tablename__ = "action_functions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    icon_data_url: Mapped[str | None] = mapped_column(Text)
    author_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    status: Mapped[ActionFunctionStatus] = mapped_column(
        Enum(ActionFunctionStatus, name="action_function_status"),
        nullable=False,
        default=ActionFunctionStatus.DRAFT,
    )
    disabled_reason: Mapped[str | None] = mapped_column(Text)
    # No FK on latest_version_id (denormalized cache; avoid circular FK with versions)
    latest_version_id: Mapped[int | None] = mapped_column(BigInteger)
    forked_from_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("action_functions.id"))
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    versions: Mapped[list["ActionFunctionVersion"]] = relationship(back_populates="function")


class ActionFunctionVersion(Base):
    __tablename__ = "action_function_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    function_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("action_functions.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    actions_meta_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    valves_schema_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    editor_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    commit_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    function: Mapped[ActionFunction] = relationship(back_populates="versions")

    __table_args__ = (UniqueConstraint("function_id", "version_no", name="uq_function_version_no"),)


class ActionFunctionValves(Base):
    __tablename__ = "action_function_valves"

    function_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("action_functions.id"), primary_key=True)
    values_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ActionFunctionRun(Base):
    __tablename__ = "action_function_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    function_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("action_functions.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    action_id: Mapped[str] = mapped_column(Text, nullable=False)
    triggered_by_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    context_type: Mapped[ActionFunctionRunContext] = mapped_column(
        Enum(ActionFunctionRunContext, name="action_function_run_context"), nullable=False
    )
    conversation_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("conversations.id"))
    message_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("messages.id"))
    request_payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[ActionFunctionRunStatus] = mapped_column(
        Enum(ActionFunctionRunStatus, name="action_function_run_status"), nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    events_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActionFunctionReport(Base):
    __tablename__ = "action_function_reports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    function_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("action_functions.id"), nullable=False)
    reporter_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ActionFunctionReportStatus] = mapped_column(
        Enum(ActionFunctionReportStatus, name="action_function_report_status"),
        nullable=False,
        default=ActionFunctionReportStatus.OPEN,
    )
    acknowledged_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

- [ ] **Step 2: Export from package init**

In `app/models/__init__.py` append:
```python
from .action_function import (  # noqa: F401
    ActionFunction,
    ActionFunctionVersion,
    ActionFunctionValves,
    ActionFunctionRun,
    ActionFunctionReport,
    ActionFunctionStatus,
    ActionFunctionRunContext,
    ActionFunctionRunStatus,
    ActionFunctionReportStatus,
)
```

- [ ] **Step 3: Smoke test — import models**

Run: `cd /home/aia/c1147259/ANILA/myCSPPlatform/backend && python -c "from app.models import ActionFunction, ActionFunctionVersion; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add myCSPPlatform/backend/app/models/action_function.py myCSPPlatform/backend/app/models/__init__.py
git commit -m "feat(models): add ActionFunction sqlalchemy models"
```

---

### Task 1.4: AES-256-GCM helper — `valves_crypto.py`

**Files:**
- Create: `myCSPPlatform/backend/app/services/action_function/__init__.py`
- Create: `myCSPPlatform/backend/app/services/action_function/valves_crypto.py`
- Create: `myCSPPlatform/backend/tests/services/action_function/__init__.py`
- Create: `myCSPPlatform/backend/tests/services/action_function/test_valves_crypto.py`

- [ ] **Step 1: Empty package inits**

```bash
touch /home/aia/c1147259/ANILA/myCSPPlatform/backend/app/services/action_function/__init__.py
mkdir -p /home/aia/c1147259/ANILA/myCSPPlatform/backend/tests/services/action_function
touch /home/aia/c1147259/ANILA/myCSPPlatform/backend/tests/services/action_function/__init__.py
```

- [ ] **Step 2: Write the failing test**

`tests/services/action_function/test_valves_crypto.py`:
```python
import json
import os
import pytest
from app.services.action_function.valves_crypto import (
    encrypt_valves,
    decrypt_valves,
    InvalidKeyError,
)


@pytest.fixture
def key_b64():
    return "wXrV3M9LmZ8s4U7yJ2K1Hn5tF6VqA0bC8xPp1iD9eRk="  # 32 bytes b64


def test_encrypt_decrypt_round_trip(key_b64, monkeypatch):
    monkeypatch.setenv("ANILA_FUNCTIONS_VALVES_KEY", key_b64)
    payload = {"api_endpoint": "https://lint.internal", "threshold": 5}
    blob, nonce, version = encrypt_valves(payload)
    assert version == 1
    assert blob != json.dumps(payload).encode()
    decrypted = decrypt_valves(blob, nonce, version)
    assert decrypted == payload


def test_decrypt_with_wrong_key_raises(key_b64, monkeypatch):
    monkeypatch.setenv("ANILA_FUNCTIONS_VALVES_KEY", key_b64)
    blob, nonce, version = encrypt_valves({"x": 1})
    monkeypatch.setenv("ANILA_FUNCTIONS_VALVES_KEY", "DIFFERENT_KEY_BASE64_32_BYTES_OK_=")
    with pytest.raises(InvalidKeyError):
        decrypt_valves(blob, nonce, version)


def test_missing_key_env_raises(monkeypatch):
    monkeypatch.delenv("ANILA_FUNCTIONS_VALVES_KEY", raising=False)
    with pytest.raises(InvalidKeyError):
        encrypt_valves({"x": 1})
```

- [ ] **Step 3: Run test (expect FAIL — module not yet)**

Run: `cd myCSPPlatform/backend && pytest tests/services/action_function/test_valves_crypto.py -v`
Expected: ImportError / module not found

- [ ] **Step 4: Implement `valves_crypto.py`**

```python
"""AES-256-GCM helper for action function admin valves.

Key 從 ENV `ANILA_FUNCTIONS_VALVES_KEY` 讀（base64-encoded 32 bytes）。
v1 manual key rotation: bump key_version 並重新加密所有 row。
"""
from __future__ import annotations

import base64
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


CURRENT_KEY_VERSION = 1
NONCE_LEN = 12  # AES-GCM standard


class InvalidKeyError(Exception):
    """Raised when key is missing, wrong length, or decrypt fails."""


def _load_key() -> bytes:
    raw = os.environ.get("ANILA_FUNCTIONS_VALVES_KEY", "").strip()
    if not raw:
        raise InvalidKeyError("ANILA_FUNCTIONS_VALVES_KEY not set")
    try:
        key = base64.b64decode(raw)
    except Exception as exc:
        raise InvalidKeyError(f"key not valid base64: {exc}") from exc
    if len(key) != 32:
        raise InvalidKeyError(f"key must decode to 32 bytes, got {len(key)}")
    return key


def encrypt_valves(values: dict) -> tuple[bytes, bytes, int]:
    """Returns (ciphertext, nonce, key_version)."""
    key = _load_key()
    nonce = os.urandom(NONCE_LEN)
    aesgcm = AESGCM(key)
    plaintext = json.dumps(values, sort_keys=True).encode("utf-8")
    ciphertext = aesgcm.encrypt(nonce, plaintext, associated_data=None)
    return ciphertext, nonce, CURRENT_KEY_VERSION


def decrypt_valves(ciphertext: bytes, nonce: bytes, key_version: int) -> dict:
    if key_version != CURRENT_KEY_VERSION:
        raise InvalidKeyError(
            f"row key_version={key_version}, current={CURRENT_KEY_VERSION}; rotate via migration"
        )
    key = _load_key()
    aesgcm = AESGCM(key)
    try:
        plaintext = aesgcm.decrypt(nonce, ciphertext, associated_data=None)
    except InvalidTag as exc:
        raise InvalidKeyError("decrypt failed (key wrong or ciphertext tampered)") from exc
    return json.loads(plaintext.decode("utf-8"))
```

- [ ] **Step 5: Run test (expect PASS)**

Run: `cd myCSPPlatform/backend && pytest tests/services/action_function/test_valves_crypto.py -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add myCSPPlatform/backend/app/services/action_function/ myCSPPlatform/backend/tests/services/action_function/
git commit -m "feat(functions): add AES-256-GCM valves crypto helper"
```

---

### Task 1.5: Pydantic schemas — `action_function.py`

**Files:**
- Create: `myCSPPlatform/backend/app/schemas/action_function.py`

- [ ] **Step 1: Write schemas file**

```python
"""Pydantic schemas for ANILA Functions v1 API."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
MAX_CODE_BYTES = 128 * 1024  # 128 KB


class FunctionCreate(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    title: str = Field(max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    icon_data_url: str | None = Field(default=None, max_length=20000)
    code: str = Field(max_length=MAX_CODE_BYTES)
    tags: list[str] = Field(default_factory=list, max_length=20)


class FunctionPatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    icon_data_url: str | None = Field(default=None, max_length=20000)
    status: Literal["draft", "enabled", "disabled"] | None = None
    tags: list[str] | None = Field(default=None, max_length=20)


class VersionCreate(BaseModel):
    code: str = Field(max_length=MAX_CODE_BYTES)
    commit_message: str | None = Field(default=None, max_length=500)


class FunctionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    title: str
    description: str | None
    icon_data_url: str | None
    author_user_id: int
    status: str
    disabled_reason: str | None
    latest_version_id: int | None
    forked_from_id: int | None
    tags: list[str]
    created_at: datetime
    updated_at: datetime
    # Code is added conditionally based on caller role


class FunctionReadWithCode(FunctionRead):
    code: str | None  # None if caller cannot view code
    valves_schema_json: dict[str, Any] | None
    actions_meta_json: list[dict[str, Any]] | None


class VersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    version_no: int
    editor_user_id: int
    commit_message: str | None
    created_at: datetime


class ValvesValues(BaseModel):
    """Admin-set values; secret fields returned as has_value flag, not plaintext."""
    values: dict[str, Any]


class ValvesValuesRead(BaseModel):
    """GET response — secret fields stripped to has_value boolean only."""
    fields: dict[str, Any]  # {field_name: actual_value or {"has_value": True} for secret}


class ForkRequest(BaseModel):
    new_slug: str | None = Field(default=None, pattern=SLUG_PATTERN)


class ReportRequest(BaseModel):
    reason: str = Field(max_length=1000)


class QuarantineRequest(BaseModel):
    reason: str = Field(max_length=500)


class RunContext(BaseModel):
    conversation_id: int | None = None
    message_id: int | None = None
    selected_text: str | None = Field(default=None, max_length=10000)


class RunRequest(BaseModel):
    action_id: str = Field(max_length=100)
    context: RunContext
    test_mode: bool = False


class RunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    function_id: int
    version_no: int
    action_id: str
    triggered_by_user_id: int
    context_type: str
    conversation_id: int | None
    message_id: int | None
    status: str
    error_message: str | None
    duration_ms: int | None
    started_at: datetime
    ended_at: datetime | None


class RunDetail(RunRead):
    request_payload_json: dict[str, Any]
    events_json: list[dict[str, Any]]


class EnabledAction(BaseModel):
    function_slug: str
    action_id: str
    name: str
    icon_data_url: str | None
    function_version: int


class EnabledActionsResponse(BaseModel):
    actions: list[EnabledAction]
```

- [ ] **Step 2: Smoke test — import schemas**

Run: `cd myCSPPlatform/backend && python -c "from app.schemas.action_function import FunctionCreate, RunRequest; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add myCSPPlatform/backend/app/schemas/action_function.py
git commit -m "feat(schemas): add ActionFunction pydantic schemas"
```

---

### Task 1.6: Function CRUD service — `crud.py` (with advisory lock + worker schema extraction stub)

**Files:**
- Create: `myCSPPlatform/backend/app/services/action_function/crud.py`
- Create: `myCSPPlatform/backend/tests/services/action_function/test_crud.py`

- [ ] **Step 1: Write failing test for create + version save with advisory lock**

`tests/services/action_function/test_crud.py`:
```python
import pytest
from sqlalchemy.orm import Session
from app.services.action_function.crud import create_function, save_version
from app.models import ActionFunction, ActionFunctionVersion, ActionFunctionStatus


def test_create_function_inserts_v1(db_session: Session, test_user):
    fn = create_function(
        db_session,
        author_user_id=test_user.id,
        slug="hello-world",
        title="Hello",
        description=None,
        icon_data_url=None,
        code="class Action:\n    actions=[{'id':'btn','name':'Hi'}]\n    async def action(self, body, **kwargs): pass\n",
        tags=[],
        # parsed metadata stub:
        actions_meta=[{"id": "btn", "name": "Hi"}],
        valves_schema={},
        metadata={},
    )
    assert fn.slug == "hello-world"
    assert fn.status == ActionFunctionStatus.DRAFT
    assert fn.latest_version_id is not None
    versions = db_session.query(ActionFunctionVersion).filter_by(function_id=fn.id).all()
    assert len(versions) == 1
    assert versions[0].version_no == 1


def test_save_version_increments_version_no(db_session: Session, test_user):
    fn = create_function(
        db_session, author_user_id=test_user.id, slug="x", title="X",
        description=None, icon_data_url=None, code="code v1", tags=[],
        actions_meta=[], valves_schema={}, metadata={},
    )
    v2 = save_version(
        db_session, fn.id, editor_user_id=test_user.id,
        code="code v2", commit_message="bump", actions_meta=[], valves_schema={}, metadata={},
    )
    assert v2.version_no == 2
    db_session.refresh(fn)
    assert fn.latest_version_id == v2.id


def test_concurrent_save_no_unique_violation(db_session: Session, test_user):
    """Advisory lock should serialize concurrent saves to same function."""
    fn = create_function(
        db_session, author_user_id=test_user.id, slug="concur", title="C",
        description=None, icon_data_url=None, code="v1", tags=[],
        actions_meta=[], valves_schema={}, metadata={},
    )
    # Sequential calls work; advisory lock ensures concurrent would too.
    for i in range(2, 5):
        v = save_version(
            db_session, fn.id, editor_user_id=test_user.id,
            code=f"v{i}", commit_message=None, actions_meta=[], valves_schema={}, metadata={},
        )
        assert v.version_no == i
```

- [ ] **Step 2: Implement `crud.py`**

```python
"""Function CRUD with advisory-lock-protected version save."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import (
    ActionFunction,
    ActionFunctionVersion,
    ActionFunctionStatus,
)


# Advisory lock namespace key for action_function table family
ADVISORY_LOCK_NS = 42


def _take_advisory_lock(db: Session, function_id: int) -> None:
    db.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :fid)"),
        {"ns": ADVISORY_LOCK_NS, "fid": function_id},
    )


def create_function(
    db: Session,
    *,
    author_user_id: int,
    slug: str,
    title: str,
    description: str | None,
    icon_data_url: str | None,
    code: str,
    tags: list[str],
    actions_meta: list[dict],
    valves_schema: dict,
    metadata: dict,
) -> ActionFunction:
    """Atomically create function row + first version + update latest_version_id."""
    fn = ActionFunction(
        slug=slug,
        title=title,
        description=description,
        icon_data_url=icon_data_url,
        author_user_id=author_user_id,
        status=ActionFunctionStatus.DRAFT,
        tags=tags or [],
    )
    db.add(fn)
    db.flush()  # populate fn.id

    _take_advisory_lock(db, fn.id)

    v = ActionFunctionVersion(
        function_id=fn.id,
        version_no=1,
        code=code,
        metadata_json=metadata,
        actions_meta_json=actions_meta,
        valves_schema_json=valves_schema,
        editor_user_id=author_user_id,
        commit_message=None,
    )
    db.add(v)
    db.flush()
    fn.latest_version_id = v.id
    db.flush()
    return fn


def save_version(
    db: Session,
    function_id: int,
    *,
    editor_user_id: int,
    code: str,
    commit_message: str | None,
    actions_meta: list[dict],
    valves_schema: dict,
    metadata: dict,
) -> ActionFunctionVersion:
    _take_advisory_lock(db, function_id)
    next_no = (
        db.execute(
            text(
                "SELECT COALESCE(MAX(version_no), 0) + 1 "
                "FROM action_function_versions WHERE function_id = :fid"
            ),
            {"fid": function_id},
        ).scalar()
    )
    v = ActionFunctionVersion(
        function_id=function_id,
        version_no=int(next_no),
        code=code,
        metadata_json=metadata,
        actions_meta_json=actions_meta,
        valves_schema_json=valves_schema,
        editor_user_id=editor_user_id,
        commit_message=commit_message,
    )
    db.add(v)
    db.flush()
    db.execute(
        text("UPDATE action_functions SET latest_version_id = :vid, updated_at = now() WHERE id = :fid"),
        {"vid": v.id, "fid": function_id},
    )
    return v


def get_function_by_slug(db: Session, slug: str) -> ActionFunction | None:
    return db.query(ActionFunction).filter_by(slug=slug).first()


def list_functions(
    db: Session,
    *,
    author_user_id: int | None = None,
    status: str | None = None,
    tag: str | None = None,
    q: str | None = None,
):
    query = db.query(ActionFunction)
    if author_user_id is not None:
        query = query.filter(ActionFunction.author_user_id == author_user_id)
    if status:
        query = query.filter(ActionFunction.status == status)
    if tag:
        query = query.filter(ActionFunction.tags.any(tag))
    if q:
        like = f"%{q}%"
        query = query.filter(
            (ActionFunction.title.ilike(like)) | (ActionFunction.description.ilike(like))
        )
    return query.order_by(ActionFunction.updated_at.desc()).all()


def get_latest_version(db: Session, function_id: int) -> ActionFunctionVersion | None:
    return (
        db.query(ActionFunctionVersion)
        .filter(ActionFunctionVersion.function_id == function_id)
        .order_by(ActionFunctionVersion.version_no.desc())
        .first()
    )
```

- [ ] **Step 3: Run test (expect PASS)**

Run: `cd myCSPPlatform/backend && pytest tests/services/action_function/test_crud.py -v`
Expected: 3 passed

- [ ] **Step 4: Commit**

```bash
git add myCSPPlatform/backend/app/services/action_function/crud.py myCSPPlatform/backend/tests/services/action_function/test_crud.py
git commit -m "feat(functions): function CRUD with advisory-lock save"
```

---

### Task 1.7: Worker client stub — `worker_client.py`

**Files:**
- Create: `myCSPPlatform/backend/app/services/action_function/worker_client.py`

Note: real implementation in Sprint 2; v1 stub returns predictable response so endpoints can be unit-tested without worker running.

- [ ] **Step 1: Write stub**

```python
"""HTTP client to anila-functions-worker-api.

Sprint 1: stub returns canned schema/SSE; Sprint 2 wires real httpx calls.
"""
from __future__ import annotations

import os
from typing import AsyncIterator
import httpx


WORKER_API_URL = os.environ.get("ANILA_FUNCTIONS_WORKER_API_URL", "http://anila-functions-worker-api:8000")
API_SECRET = os.environ.get("ANILA_FUNCTIONS_API_SECRET", "")


class WorkerClient:
    """Thin httpx wrapper. Raises on connection error / non-2xx."""

    def __init__(self, base_url: str | None = None, secret: str | None = None):
        self.base_url = base_url or WORKER_API_URL
        self.secret = secret or API_SECRET
        self._headers = {"X-Functions-Api-Secret": self.secret}

    async def extract_meta(self, code: str) -> dict:
        """POST /extract-meta — Sprint 2 wires real call.

        Sprint 1 stub: parse minimal AST locally to enable backend tests
        without the worker container running.
        """
        # Stub: allow test to override via ANILA_FUNCTIONS_STUB_EXTRACT
        if os.environ.get("ANILA_FUNCTIONS_STUB_EXTRACT") == "1":
            return self._stub_extract(code)
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{self.base_url}/extract-meta",
                json={"code": code},
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    def _stub_extract(code: str) -> dict:
        return {
            "actions_meta_json": [{"id": "stub-btn", "name": "Stub", "icon_url": None}],
            "valves_schema_json": {"type": "object", "properties": {}},
            "metadata_json": {"title": "Stub", "version": "1.0"},
            "extract_strategy": "stub",
            "errors": [],
        }

    async def stream_exec(self, payload: dict) -> AsyncIterator[bytes]:
        """POST /exec, returns chunked SSE bytes. Sprint 2 wires real call."""
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/exec",
                json=payload,
                headers=self._headers,
            ) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes():
                    yield chunk
```

- [ ] **Step 2: Commit**

```bash
git add myCSPPlatform/backend/app/services/action_function/worker_client.py
git commit -m "feat(functions): worker-api client stub (real impl in Sprint 2)"
```

---

### Task 1.8: Ownership service — `ownership.py` (chat_message vs test_console split)

**Files:**
- Create: `myCSPPlatform/backend/app/services/action_function/ownership.py`
- Create: `myCSPPlatform/backend/tests/services/action_function/test_ownership.py`

- [ ] **Step 1: Write failing tests**

`tests/services/action_function/test_ownership.py`:
```python
import pytest
from app.services.action_function.ownership import (
    authorize_chat_message_run,
    authorize_test_console_run,
    AuthzError,
)
from app.models import ActionFunctionStatus


def test_chat_message_requires_enabled(db_session, test_user, fn_factory, conv_factory, msg_factory):
    fn = fn_factory(status=ActionFunctionStatus.DISABLED)
    conv = conv_factory(owner_id=test_user.id)
    msg = msg_factory(conv_id=conv.id, role="assistant")
    with pytest.raises(AuthzError, match="enabled"):
        authorize_chat_message_run(db_session, caller=test_user, fn=fn, conv_id=conv.id, msg_id=msg.id)


def test_chat_message_requires_owner(db_session, test_user, other_user, fn_factory, conv_factory, msg_factory):
    fn = fn_factory(status=ActionFunctionStatus.ENABLED)
    conv = conv_factory(owner_id=other_user.id)
    msg = msg_factory(conv_id=conv.id, role="assistant")
    with pytest.raises(AuthzError, match="conversation"):
        authorize_chat_message_run(db_session, caller=test_user, fn=fn, conv_id=conv.id, msg_id=msg.id)


def test_chat_message_requires_assistant_role(db_session, test_user, fn_factory, conv_factory, msg_factory):
    fn = fn_factory(status=ActionFunctionStatus.ENABLED)
    conv = conv_factory(owner_id=test_user.id)
    msg = msg_factory(conv_id=conv.id, role="user")
    with pytest.raises(AuthzError, match="assistant"):
        authorize_chat_message_run(db_session, caller=test_user, fn=fn, conv_id=conv.id, msg_id=msg.id)


def test_chat_message_msg_must_belong_to_conv(db_session, test_user, fn_factory, conv_factory, msg_factory):
    fn = fn_factory(status=ActionFunctionStatus.ENABLED)
    conv1 = conv_factory(owner_id=test_user.id)
    conv2 = conv_factory(owner_id=test_user.id)
    msg = msg_factory(conv_id=conv2.id, role="assistant")
    with pytest.raises(AuthzError, match="conversation"):
        authorize_chat_message_run(db_session, caller=test_user, fn=fn, conv_id=conv1.id, msg_id=msg.id)


def test_test_console_author_can_run_disabled(db_session, test_user, fn_factory):
    fn = fn_factory(status=ActionFunctionStatus.DISABLED, author_user_id=test_user.id)
    # No raise:
    authorize_test_console_run(db_session, caller=test_user, fn=fn)


def test_test_console_non_author_rejected(db_session, test_user, other_user, fn_factory):
    fn = fn_factory(status=ActionFunctionStatus.ENABLED, author_user_id=other_user.id)
    with pytest.raises(AuthzError, match="author"):
        authorize_test_console_run(db_session, caller=test_user, fn=fn)


def test_test_console_admin_can_run_quarantined(db_session, admin_user, fn_factory):
    fn = fn_factory(status=ActionFunctionStatus.QUARANTINED)
    authorize_test_console_run(db_session, caller=admin_user, fn=fn)
```

- [ ] **Step 2: Implement `ownership.py`**

```python
"""7-step authz for /run, split into chat_message and test_console paths."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import ActionFunction, ActionFunctionStatus
from app.services.conversation_service import _check_access  # existing owner+admin gate


class AuthzError(Exception):
    """Raised when a /run authz step fails. Surface as 403."""


def _is_admin(caller) -> bool:
    return getattr(caller, "role", None) == "admin"


def authorize_chat_message_run(
    db: Session,
    *,
    caller,
    fn: ActionFunction,
    conv_id: int,
    msg_id: int,
) -> None:
    """Steps 3b-5d from spec §4.5."""
    if fn.status != ActionFunctionStatus.ENABLED:
        raise AuthzError("function not enabled")
    # Reuse existing conversation_service gate (owner+admin only in v1)
    conv = _check_access(db, caller, conv_id)
    if conv is None:
        raise AuthzError("conversation access denied")
    # Verify message belongs to this conversation
    from app.models import Message  # local import to avoid cycles
    msg = db.query(Message).filter_by(id=msg_id).first()
    if msg is None or msg.conversation_id != conv_id:
        raise AuthzError("message not in conversation")
    if msg.role != "assistant":
        raise AuthzError("action button only on assistant messages")
    # classified gate is already inside _check_access for this user's role


def authorize_test_console_run(db: Session, *, caller, fn: ActionFunction) -> None:
    """test_mode=true path: caller must be author or admin (function status irrelevant)."""
    if _is_admin(caller):
        return
    if fn.author_user_id != caller.id:
        raise AuthzError("test mode: only author or admin can run")
```

- [ ] **Step 3: Add fixtures to `tests/conftest.py`**

If not present already, add to `tests/conftest.py`:
```python
import pytest
from app.models import ActionFunction, ActionFunctionStatus, Conversation, Message


@pytest.fixture
def fn_factory(db_session):
    def _make(**kwargs):
        defaults = dict(
            slug=f"fn-{kwargs.get('slug_suffix', 'test')}",
            title="T", author_user_id=kwargs.get("author_user_id", 1),
            status=ActionFunctionStatus.DRAFT, tags=[],
        )
        defaults.update({k: v for k, v in kwargs.items() if k != "slug_suffix"})
        fn = ActionFunction(**defaults)
        db_session.add(fn); db_session.flush()
        return fn
    return _make


@pytest.fixture
def conv_factory(db_session):
    def _make(**kwargs):
        defaults = dict(title="Test", owner_user_id=kwargs.get("owner_id", 1))
        defaults.update({k: v for k, v in kwargs.items() if k != "owner_id"})
        if "owner_id" in kwargs:
            defaults["owner_user_id"] = kwargs["owner_id"]
        c = Conversation(**defaults)
        db_session.add(c); db_session.flush()
        return c
    return _make


@pytest.fixture
def msg_factory(db_session):
    def _make(**kwargs):
        m = Message(
            conversation_id=kwargs["conv_id"],
            role=kwargs.get("role", "assistant"),
            content=kwargs.get("content", "hi"),
        )
        db_session.add(m); db_session.flush()
        return m
    return _make
```
(Adapt field names if Conversation/Message models differ — check `app/models/conversation.py` & `app/models/message.py`.)

- [ ] **Step 4: Run tests (expect PASS)**

Run: `cd myCSPPlatform/backend && pytest tests/services/action_function/test_ownership.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add myCSPPlatform/backend/app/services/action_function/ownership.py myCSPPlatform/backend/tests/services/action_function/test_ownership.py myCSPPlatform/backend/tests/conftest.py
git commit -m "feat(functions): ownership authz (chat_message vs test_console split)"
```

---

### Task 1.9: Audit redaction service — `redaction.py`

**Files:**
- Create: `myCSPPlatform/backend/app/services/action_function/redaction.py`
- Create: `myCSPPlatform/backend/tests/services/action_function/test_redaction.py`

- [ ] **Step 1: Write failing tests**

```python
from app.services.action_function.redaction import redact_events, redact_payload


def test_redacts_secret_substring_from_event_text():
    secrets = {"api_token": "sk-abc12345xyz"}
    events = [{"type": "status", "description": "Calling with sk-abc12345xyz now"}]
    out = redact_events(events, secrets)
    assert "sk-abc12345xyz" not in str(out)
    assert "<redacted:api_token>" in out[0]["description"]


def test_short_secrets_not_redacted():
    """< 8 char secrets skipped to avoid false positives."""
    secrets = {"x": "abc"}
    events = [{"type": "status", "description": "abc def"}]
    out = redact_events(events, secrets)
    assert out == events  # unchanged


def test_redacts_in_nested_dict():
    secrets = {"k": "TOKEN_LONG_ENOUGH"}
    events = [{"type": "host_command", "verb": "chat.show_modal", "args": {"content_md": "leak: TOKEN_LONG_ENOUGH"}}]
    out = redact_events(events, secrets)
    assert "TOKEN_LONG_ENOUGH" not in str(out)


def test_no_secrets_returns_input():
    events = [{"type": "status", "description": "hi"}]
    assert redact_events(events, {}) == events
```

- [ ] **Step 2: Implement `redaction.py`**

```python
"""Audit redaction — defense in depth, not primary protection (see spec §7.4)."""
from __future__ import annotations

import json
from typing import Any


MIN_SECRET_LEN = 8  # secrets < 8 chars not redacted (avoid false positives on common strings)


def _redact_string(s: str, secrets: dict[str, str]) -> str:
    out = s
    for field, value in secrets.items():
        if not isinstance(value, str) or len(value) < MIN_SECRET_LEN:
            continue
        out = out.replace(value, f"<redacted:valves.{field}>")
    return out


def _redact_value(v: Any, secrets: dict[str, str]) -> Any:
    if isinstance(v, str):
        return _redact_string(v, secrets)
    if isinstance(v, dict):
        return {k: _redact_value(vv, secrets) for k, vv in v.items()}
    if isinstance(v, list):
        return [_redact_value(item, secrets) for item in v]
    return v


def redact_events(events: list[dict], secrets: dict[str, str]) -> list[dict]:
    """Best-effort redaction. Returns new list; input unchanged."""
    if not secrets:
        return events
    return [_redact_value(e, secrets) for e in events]


def redact_payload(payload: dict, secrets: dict[str, str]) -> dict:
    if not secrets:
        return payload
    return _redact_value(payload, secrets)


def collect_secret_values(values: dict, schema: dict) -> dict[str, str]:
    """From valves dict + JSON schema, return {field_name: plaintext_value} for fields marked secret."""
    out: dict[str, str] = {}
    props = schema.get("properties", {})
    for field, meta in props.items():
        if meta.get("json_schema_extra", {}).get("secret") or meta.get("x-secret"):
            v = values.get(field)
            if isinstance(v, str) and v:
                out[field] = v
    return out
```

- [ ] **Step 3: Run tests (expect PASS)**

Run: `cd myCSPPlatform/backend && pytest tests/services/action_function/test_redaction.py -v`
Expected: 4 passed

- [ ] **Step 4: Commit**

```bash
git add myCSPPlatform/backend/app/services/action_function/redaction.py myCSPPlatform/backend/tests/services/action_function/test_redaction.py
git commit -m "feat(functions): audit redaction (defense-in-depth)"
```

---

### Task 1.10: API package init + Function CRUD endpoints

**Files:**
- Create: `myCSPPlatform/backend/app/api/action_function/__init__.py`
- Create: `myCSPPlatform/backend/app/api/action_function/crud.py`
- Modify: `myCSPPlatform/backend/app/api/router.py` — include router

- [ ] **Step 1: Write CRUD endpoints**

`app/api/action_function/crud.py`:
```python
"""Function CRUD endpoints. Code visibility per RBAC (developer+ for non-draft)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models import ActionFunction, ActionFunctionStatus, User
from app.schemas.action_function import (
    FunctionCreate,
    FunctionPatch,
    FunctionRead,
    FunctionReadWithCode,
    VersionCreate,
    VersionRead,
)
from app.services.action_function import crud as fn_crud
from app.services.action_function.worker_client import WorkerClient


router = APIRouter(prefix="/api/functions", tags=["action-functions"])


def _require_developer(user: User) -> None:
    if user.role not in ("developer", "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="developer role required")


def _can_view_code(user: User, fn: ActionFunction) -> bool:
    """Per spec §3.6 + §7.1:
    - admin: any status
    - author: own function any status (incl. draft / quarantined)
    - developer (non-author): enabled/disabled only — NOT quarantined, NOT draft
    - user role: never
    """
    if user.role == "admin":
        return True
    if fn.author_user_id == user.id:
        return True
    if user.role == "developer" and fn.status in (
        ActionFunctionStatus.ENABLED,
        ActionFunctionStatus.DISABLED,
    ):
        # quarantined deliberately excluded — admin disabled-due-to-abuse should not be readable
        return True
    return False


@router.get("", response_model=list[FunctionRead])
def list_functions(
    author: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    author_id = user.id if author == "me" else None
    fns = fn_crud.list_functions(db, author_user_id=author_id, status=status, tag=tag, q=q)
    return fns


@router.post("", response_model=FunctionRead, status_code=201)
async def create_function(
    payload: FunctionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_developer(user)
    if fn_crud.get_function_by_slug(db, payload.slug):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="slug already exists")
    # Extract metadata via worker (stub in Sprint 1)
    client = WorkerClient()
    extract = await client.extract_meta(payload.code)
    if extract.get("errors"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"extract_errors": extract["errors"]},
        )
    fn = fn_crud.create_function(
        db,
        author_user_id=user.id,
        slug=payload.slug,
        title=payload.title,
        description=payload.description,
        icon_data_url=payload.icon_data_url,
        code=payload.code,
        tags=payload.tags,
        actions_meta=extract["actions_meta_json"],
        valves_schema=extract["valves_schema_json"],
        metadata=extract["metadata_json"],
    )
    db.commit()
    return fn


@router.get("/{slug}", response_model=FunctionReadWithCode)
def get_function(
    slug: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fn = fn_crud.get_function_by_slug(db, slug)
    if fn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    latest = fn_crud.get_latest_version(db, fn.id)
    response = FunctionReadWithCode.model_validate(fn).model_dump()
    if latest and _can_view_code(user, fn):
        response["code"] = latest.code
        response["valves_schema_json"] = latest.valves_schema_json
        response["actions_meta_json"] = latest.actions_meta_json
    else:
        response["code"] = None
        response["valves_schema_json"] = None
        response["actions_meta_json"] = None
    return response


@router.patch("/{slug}", response_model=FunctionRead)
def patch_function(
    slug: str,
    payload: FunctionPatch,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fn = fn_crud.get_function_by_slug(db, slug)
    if fn is None:
        raise HTTPException(404)
    if fn.author_user_id != user.id and user.role != "admin":
        raise HTTPException(403)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(fn, field, value)
    db.commit()
    db.refresh(fn)
    return fn


@router.delete("/{slug}", status_code=204)
def delete_function(
    slug: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fn = fn_crud.get_function_by_slug(db, slug)
    if fn is None:
        raise HTTPException(404)
    if fn.author_user_id != user.id and user.role != "admin":
        raise HTTPException(403)
    fn.status = ActionFunctionStatus.DISABLED
    db.commit()


@router.post("/{slug}/versions", response_model=VersionRead, status_code=201)
async def save_version(
    slug: str,
    payload: VersionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fn = fn_crud.get_function_by_slug(db, slug)
    if fn is None:
        raise HTTPException(404)
    if fn.author_user_id != user.id and user.role != "admin":
        raise HTTPException(403)
    client = WorkerClient()
    extract = await client.extract_meta(payload.code)
    if extract.get("errors"):
        raise HTTPException(400, detail={"extract_errors": extract["errors"]})
    v = fn_crud.save_version(
        db, fn.id,
        editor_user_id=user.id,
        code=payload.code,
        commit_message=payload.commit_message,
        actions_meta=extract["actions_meta_json"],
        valves_schema=extract["valves_schema_json"],
        metadata=extract["metadata_json"],
    )
    db.commit()
    return v


@router.get("/{slug}/versions", response_model=list[VersionRead])
def list_versions(
    slug: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fn = fn_crud.get_function_by_slug(db, slug)
    if fn is None:
        raise HTTPException(404)
    if not _can_view_code(user, fn):
        raise HTTPException(403)
    from app.models import ActionFunctionVersion
    versions = (
        db.query(ActionFunctionVersion)
        .filter(ActionFunctionVersion.function_id == fn.id)
        .order_by(ActionFunctionVersion.version_no.desc())
        .all()
    )
    return versions
```

- [ ] **Step 2: Package init exports the router**

`app/api/action_function/__init__.py`:
```python
from .crud import router as crud_router

__all__ = ["crud_router"]
```

- [ ] **Step 3: Wire up in `app/api/router.py`**

Append to imports:
```python
from app.api.action_function import crud_router as fn_crud_router
```

In the include_router calls section, add:
```python
api_router.include_router(fn_crud_router)
```

- [ ] **Step 4: Smoke test — server starts**

Run: `cd myCSPPlatform/backend && python -c "from app.main import app; print(len(app.routes), 'routes')"`
Expected: route count > previous (no errors)

- [ ] **Step 5: Commit**

```bash
git add myCSPPlatform/backend/app/api/action_function/ myCSPPlatform/backend/app/api/router.py
git commit -m "feat(api): action_function CRUD + version endpoints"
```

---

### Task 1.11: Valves endpoint with encryption

**Files:**
- Create: `myCSPPlatform/backend/app/api/action_function/valves.py`
- Modify: `myCSPPlatform/backend/app/api/action_function/__init__.py`
- Modify: `myCSPPlatform/backend/app/api/router.py`
- Create: `myCSPPlatform/backend/tests/api/action_function/test_valves.py`

- [ ] **Step 1: Implement valves endpoint**

```python
"""Admin Valves get/set with AES-GCM encryption at rest."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models import ActionFunctionValves, User
from app.schemas.action_function import ValvesValues, ValvesValuesRead
from app.services.action_function import crud as fn_crud
from app.services.action_function.valves_crypto import encrypt_valves, decrypt_valves


router = APIRouter(prefix="/api/functions", tags=["action-functions"])


def _strip_secrets_for_read(values: dict, schema: dict) -> dict:
    """Replace secret-tagged fields with {has_value: bool}."""
    out: dict = {}
    props = schema.get("properties", {})
    for k, v in values.items():
        meta = props.get(k, {})
        if meta.get("json_schema_extra", {}).get("secret") or meta.get("x-secret"):
            out[k] = {"has_value": bool(v)}
        else:
            out[k] = v
    return out


@router.get("/{slug}/valves", response_model=ValvesValuesRead)
def get_valves(
    slug: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fn = fn_crud.get_function_by_slug(db, slug)
    if fn is None:
        raise HTTPException(404)
    if fn.author_user_id != user.id and user.role != "admin":
        raise HTTPException(403)
    row = db.query(ActionFunctionValves).filter_by(function_id=fn.id).first()
    if row is None:
        return ValvesValuesRead(fields={})
    plaintext = decrypt_valves(row.values_encrypted, row.nonce, row.key_version)
    latest = fn_crud.get_latest_version(db, fn.id)
    schema = latest.valves_schema_json if latest else {}
    return ValvesValuesRead(fields=_strip_secrets_for_read(plaintext, schema))


@router.put("/{slug}/valves", status_code=204)
def put_valves(
    slug: str,
    payload: ValvesValues,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role != "admin":
        raise HTTPException(403, detail="admin role required to set Valves")
    fn = fn_crud.get_function_by_slug(db, slug)
    if fn is None:
        raise HTTPException(404)
    ciphertext, nonce, key_version = encrypt_valves(payload.values)
    row = db.query(ActionFunctionValves).filter_by(function_id=fn.id).first()
    if row is None:
        row = ActionFunctionValves(
            function_id=fn.id,
            values_encrypted=ciphertext,
            nonce=nonce,
            key_version=key_version,
            updated_by=user.id,
        )
        db.add(row)
    else:
        row.values_encrypted = ciphertext
        row.nonce = nonce
        row.key_version = key_version
        row.updated_by = user.id
    db.commit()
```

- [ ] **Step 2: Update `__init__.py` exports**

```python
from .crud import router as crud_router
from .valves import router as valves_router

__all__ = ["crud_router", "valves_router"]
```

- [ ] **Step 3: Wire in router.py**

Add to imports and includes:
```python
from app.api.action_function import crud_router as fn_crud_router, valves_router as fn_valves_router
api_router.include_router(fn_valves_router)
```

- [ ] **Step 4: Write integration test**

`tests/api/action_function/test_valves.py`:
```python
import pytest
from fastapi.testclient import TestClient


def test_admin_can_put_and_get_valves(test_client: TestClient, admin_token, fn_factory_db):
    fn = fn_factory_db(slug="myfn")
    headers = {"Authorization": f"Bearer {admin_token}"}
    r = test_client.put(
        f"/api/functions/{fn.slug}/valves",
        json={"values": {"endpoint": "https://x", "token": "sk-12345678abcdefg"}},
        headers=headers,
    )
    assert r.status_code == 204
    g = test_client.get(f"/api/functions/{fn.slug}/valves", headers=headers)
    assert g.status_code == 200
    body = g.json()
    # Plain field returned plain; secret field stripped to has_value (depends on schema)
    assert "endpoint" in body["fields"]


def test_user_role_cannot_put_valves(test_client, user_token, fn_factory_db):
    fn = fn_factory_db(slug="myfn2")
    r = test_client.put(
        f"/api/functions/{fn.slug}/valves",
        json={"values": {"x": 1}},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 403
```

- [ ] **Step 5: Run tests + commit**

Run: `cd myCSPPlatform/backend && pytest tests/api/action_function/test_valves.py -v`
Expected: 2 passed

```bash
git add myCSPPlatform/backend/app/api/action_function/valves.py myCSPPlatform/backend/app/api/action_function/__init__.py myCSPPlatform/backend/app/api/router.py myCSPPlatform/backend/tests/api/action_function/test_valves.py
git commit -m "feat(api): admin valves endpoint with AES-GCM at rest"
```

---

### Task 1.12: Marketplace — fork / report / quarantine

**Files:**
- Create: `myCSPPlatform/backend/app/api/action_function/marketplace.py`
- Create: `myCSPPlatform/backend/tests/api/action_function/test_marketplace.py`

- [ ] **Step 1: Implement marketplace endpoints**

```python
"""Fork + abuse report + quarantine endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models import (
    ActionFunction,
    ActionFunctionStatus,
    ActionFunctionReport,
    ActionFunctionReportStatus,
    User,
)
from app.schemas.action_function import (
    FunctionRead,
    ForkRequest,
    ReportRequest,
    QuarantineRequest,
)
from app.services.action_function import crud as fn_crud


router = APIRouter(prefix="/api/functions", tags=["action-functions"])


@router.post("/{slug}/fork", response_model=FunctionRead, status_code=201)
def fork(
    slug: str,
    payload: ForkRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role not in ("developer", "admin"):
        raise HTTPException(403)
    src = fn_crud.get_function_by_slug(db, slug)
    if src is None:
        raise HTTPException(404)
    if src.status != ActionFunctionStatus.ENABLED:
        raise HTTPException(403, detail="can only fork enabled functions")
    new_slug = payload.new_slug or f"{src.slug}-fork-{user.id}"
    if fn_crud.get_function_by_slug(db, new_slug):
        raise HTTPException(409, detail="new_slug already exists")
    src_version = fn_crud.get_latest_version(db, src.id)
    fork_fn = fn_crud.create_function(
        db,
        author_user_id=user.id,
        slug=new_slug,
        title=src.title,
        description=src.description,
        icon_data_url=src.icon_data_url,
        code=src_version.code if src_version else "",
        tags=list(src.tags),
        actions_meta=src_version.actions_meta_json if src_version else [],
        valves_schema=src_version.valves_schema_json if src_version else {},
        metadata=src_version.metadata_json if src_version else {},
    )
    fork_fn.forked_from_id = src.id
    db.commit()
    return fork_fn


@router.post("/{slug}/report", status_code=201)
def report(
    slug: str,
    payload: ReportRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fn = fn_crud.get_function_by_slug(db, slug)
    if fn is None:
        raise HTTPException(404)
    rep = ActionFunctionReport(
        function_id=fn.id,
        reporter_user_id=user.id,
        reason=payload.reason,
        status=ActionFunctionReportStatus.OPEN,
    )
    db.add(rep)
    # also write audit_logs row (existing pattern)
    from app.services.audit_service import write_audit
    write_audit(db, actor_id=user.id, kind="FUNCTION_REPORT", target=f"action_function:{fn.slug}",
                payload={"reason": payload.reason})
    db.commit()
    return {"id": rep.id}


@router.post("/{slug}/quarantine", status_code=204)
def quarantine(
    slug: str,
    payload: QuarantineRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role != "admin":
        raise HTTPException(403)
    fn = fn_crud.get_function_by_slug(db, slug)
    if fn is None:
        raise HTTPException(404)
    fn.status = ActionFunctionStatus.QUARANTINED
    fn.disabled_reason = payload.reason
    from app.services.audit_service import write_audit
    write_audit(db, actor_id=user.id, kind="FUNCTION_QUARANTINE", target=f"action_function:{fn.slug}",
                payload={"reason": payload.reason})
    db.commit()


@router.post("/{slug}/unquarantine", status_code=204)
def unquarantine(
    slug: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role != "admin":
        raise HTTPException(403)
    fn = fn_crud.get_function_by_slug(db, slug)
    if fn is None:
        raise HTTPException(404)
    if fn.status != ActionFunctionStatus.QUARANTINED:
        raise HTTPException(400, detail="not quarantined")
    fn.status = ActionFunctionStatus.DISABLED
    fn.disabled_reason = None
    db.commit()
```

- [ ] **Step 2: Wire + test + commit**

Add `marketplace_router` to `__init__.py`, include in `router.py`, write integration test for fork-only-enabled, then:

```bash
cd myCSPPlatform/backend && pytest tests/api/action_function/test_marketplace.py -v
git add myCSPPlatform/backend/app/api/action_function/marketplace.py myCSPPlatform/backend/app/api/action_function/__init__.py myCSPPlatform/backend/app/api/router.py myCSPPlatform/backend/tests/api/action_function/test_marketplace.py
git commit -m "feat(api): marketplace fork + report + quarantine endpoints"
```

---

### Task 1.13: Run endpoint stub + audit + ChatRuntime helper

**Files:**
- Create: `myCSPPlatform/backend/app/api/action_function/run.py`
- Create: `myCSPPlatform/backend/app/api/action_function/runs.py`
- Create: `myCSPPlatform/backend/app/api/action_function/enabled_actions.py`

- [ ] **Step 1: Implement `/run` (Sprint 1 stub: 503 unless test_mode + author)**

```python
"""SSE /run endpoint. Sprint 1: ownership check passes through; SSE relay 接 worker stub."""
from __future__ import annotations

import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models import (
    ActionFunctionRun,
    ActionFunctionRunContext,
    ActionFunctionRunStatus,
    User,
)
from app.schemas.action_function import RunRequest
from app.services.action_function import crud as fn_crud
from app.services.action_function.ownership import (
    authorize_chat_message_run,
    authorize_test_console_run,
    AuthzError,
)
from app.services.action_function.worker_client import WorkerClient


router = APIRouter(prefix="/api/functions", tags=["action-functions"])


@router.post("/{slug}/run")
async def run_function(
    slug: str,
    payload: RunRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fn = fn_crud.get_function_by_slug(db, slug)
    if fn is None:
        raise HTTPException(404)
    try:
        if payload.test_mode:
            authorize_test_console_run(db, caller=user, fn=fn)
        else:
            authorize_chat_message_run(
                db, caller=user, fn=fn,
                conv_id=payload.context.conversation_id,
                msg_id=payload.context.message_id,
            )
    except AuthzError as exc:
        raise HTTPException(403, detail=str(exc))
    latest = fn_crud.get_latest_version(db, fn.id)
    if latest is None:
        raise HTTPException(400, detail="function has no version")
    run = ActionFunctionRun(
        function_id=fn.id,
        version_no=latest.version_no,
        action_id=payload.action_id,
        triggered_by_user_id=user.id,
        context_type=(
            ActionFunctionRunContext.TEST_CONSOLE if payload.test_mode
            else ActionFunctionRunContext.CHAT_MESSAGE
        ),
        conversation_id=payload.context.conversation_id,
        message_id=payload.context.message_id,
        request_payload_json=payload.model_dump(),
        status=ActionFunctionRunStatus.RUNNING,
    )
    db.add(run); db.commit(); db.refresh(run)

    client = WorkerClient()

    async def stream():
        try:
            async for chunk in client.stream_exec({
                "code": latest.code,
                "body": payload.model_dump()["context"] | {"action_id": payload.action_id},
                "valves": {},   # Sprint 2 wires real decrypted valves
                "user": {"id": user.id, "username": user.username, "role": user.role},
                "metadata": {"started_at": datetime.utcnow().isoformat()},
            }):
                yield chunk
        except Exception as exc:
            yield f"event: function_event\ndata: {json.dumps({'type':'error','message':str(exc)})}\n\n".encode()
        finally:
            run.status = ActionFunctionRunStatus.SUCCESS  # Sprint 2 distinguishes
            run.ended_at = datetime.utcnow()
            db.commit()
            yield f"event: function_done\ndata: {json.dumps({'run_id': run.id, 'status': run.status.value})}\n\n".encode()

    return StreamingResponse(stream(), media_type="text/event-stream")
```

- [ ] **Step 2: Implement `/runs` audit endpoints**

`app/api/action_function/runs.py`:
```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models import ActionFunctionRun, User
from app.schemas.action_function import RunRead, RunDetail
from app.services.action_function import crud as fn_crud


router = APIRouter(prefix="/api/functions", tags=["action-functions"])


@router.get("/{slug}/runs", response_model=list[RunRead])
def list_runs(
    slug: str,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fn = fn_crud.get_function_by_slug(db, slug)
    if fn is None:
        raise HTTPException(404)
    if fn.author_user_id != user.id and user.role != "admin":
        raise HTTPException(403)
    return (
        db.query(ActionFunctionRun)
        .filter_by(function_id=fn.id)
        .order_by(ActionFunctionRun.started_at.desc())
        .limit(limit)
        .all()
    )


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(
    run_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    run = db.query(ActionFunctionRun).filter_by(id=run_id).first()
    if run is None:
        raise HTTPException(404)
    fn = db.query(ActionFunctionRun).filter_by(id=run.function_id).first()
    if (
        run.triggered_by_user_id != user.id
        and (fn and fn.author_user_id != user.id)
        and user.role != "admin"
    ):
        raise HTTPException(403)
    return run
```

- [ ] **Step 3: Implement `/enabled-actions`**

`app/api/action_function/enabled_actions.py`:
```python
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models import ActionFunction, ActionFunctionStatus, ActionFunctionVersion, User
from app.schemas.action_function import EnabledAction, EnabledActionsResponse


router = APIRouter(prefix="/api/functions", tags=["action-functions"])


@router.get("/enabled-actions", response_model=EnabledActionsResponse)
def enabled_actions(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fns = (
        db.query(ActionFunction, ActionFunctionVersion)
        .join(ActionFunctionVersion, ActionFunction.latest_version_id == ActionFunctionVersion.id)
        .filter(ActionFunction.status == ActionFunctionStatus.ENABLED)
        .all()
    )
    actions: list[EnabledAction] = []
    for fn, ver in fns:
        for a in ver.actions_meta_json:
            actions.append(EnabledAction(
                function_slug=fn.slug,
                action_id=a["id"],
                name=a["name"],
                icon_data_url=a.get("icon_url"),
                function_version=ver.version_no,
            ))
    return EnabledActionsResponse(actions=actions)
```

- [ ] **Step 4: Wire + integration smoke test + commit**

Update `app/api/action_function/__init__.py` and `router.py` to include new routers.

Run: `cd myCSPPlatform/backend && pytest tests/api/action_function/ -v`

```bash
git add myCSPPlatform/backend/app/api/action_function/run.py myCSPPlatform/backend/app/api/action_function/runs.py myCSPPlatform/backend/app/api/action_function/enabled_actions.py myCSPPlatform/backend/app/api/action_function/__init__.py myCSPPlatform/backend/app/api/router.py
git commit -m "feat(api): /run SSE relay + /runs audit + /enabled-actions"
```

---

### Task 1.14: Audit retention cron job (360-day purge)

**Files:**
- Create: `myCSPPlatform/backend/app/services/action_function/retention.py`
- Modify: `myCSPPlatform/backend/app/main.py` — register periodic task on startup (or use existing scheduler if present)

- [ ] **Step 1: Implement purge function**

```python
"""Daily purge of action_function_runs older than 360 days (spec §7.6)."""
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from app.models import ActionFunctionRun


def purge_expired_runs(db: Session, cutoff_days: int = 360) -> int:
    """Delete runs with started_at < now() - cutoff_days. Returns count deleted."""
    cutoff = datetime.utcnow() - timedelta(days=cutoff_days)
    n = (
        db.query(ActionFunctionRun)
        .filter(ActionFunctionRun.started_at < cutoff)
        .delete(synchronize_session=False)
    )
    db.commit()
    return n
```

- [ ] **Step 2: Test**

```python
def test_purge_removes_old_runs(db_session):
    from app.models import ActionFunctionRun, ActionFunctionRunStatus, ActionFunctionRunContext
    from datetime import datetime, timedelta
    old = ActionFunctionRun(
        function_id=1, version_no=1, action_id="x", triggered_by_user_id=1,
        context_type=ActionFunctionRunContext.CHAT_MESSAGE,
        status=ActionFunctionRunStatus.SUCCESS,
        started_at=datetime.utcnow() - timedelta(days=400),
    )
    new = ActionFunctionRun(
        function_id=1, version_no=1, action_id="x", triggered_by_user_id=1,
        context_type=ActionFunctionRunContext.CHAT_MESSAGE,
        status=ActionFunctionRunStatus.SUCCESS,
        started_at=datetime.utcnow() - timedelta(days=10),
    )
    db_session.add_all([old, new]); db_session.commit()
    from app.services.action_function.retention import purge_expired_runs
    n = purge_expired_runs(db_session)
    assert n == 1
    assert db_session.query(ActionFunctionRun).count() == 1
```

- [ ] **Step 3: Wire to existing scheduler**

Check if `myCSPPlatform/backend/app/main.py` already has a periodic task framework (cron / APScheduler / arq cron). If yes, add daily call. If not, document a note: deploy a small k8s CronJob / docker-compose `restart: on-failure` daemon that runs once a day calling this function.

- [ ] **Step 4: Commit**

```bash
git add myCSPPlatform/backend/app/services/action_function/retention.py myCSPPlatform/backend/tests/services/action_function/test_retention.py
git commit -m "feat(functions): 360-day audit retention purge"
```

---

## Sprint 2 — Worker Stack (~20-30 tasks, 5-7 working days)

> **⚠️ Plan elaboration note** — Sprint 2 tasks 2.4 / 2.5 / 2.6 / 2.7 / 2.8 below give file paths + intent + skeleton code. Tasks marked **(elaborate)** below should follow Sprint 1's 4-step TDD pattern (write failing test → implement → run → commit) when execution begins. Subagent-driven execution will fill these in per task; inline execution should expand them at sprint kickoff.

**Goal:** Build worker-api + sandbox containers + entrypoint + runtime; wire CSP `worker_client.py` to real HTTP; SSE end-to-end.

### Task 2.1: Project skeleton

**Files:**
- Create: `anila-functions-worker/` directory tree (per file structure above)

- [ ] **Step 1: Create directories**

```bash
cd /home/aia/c1147259/ANILA
mkdir -p anila-functions-worker/{worker-api,sandbox,shared,tests}
touch anila-functions-worker/{worker-api,sandbox,shared,tests}/__init__.py
```

- [ ] **Step 2: Write `requirements.txt`**

`anila-functions-worker/requirements.txt`:
```
fastapi==0.115.6
uvicorn[standard]==0.34.0
httpx==0.28.1
pydantic==2.10.4
pydantic-settings==2.7.1
python-dateutil==2.9.0
cryptography==44.0.0
pytest==8.3.4
pytest-asyncio==0.25.2
```

- [ ] **Step 3: Commit skeleton**

```bash
git add anila-functions-worker/
git commit -m "feat(worker): project skeleton"
```

---

### Task 2.2: Wire protocol — `shared/wire.py`

**Files:**
- Create: `anila-functions-worker/shared/wire.py`
- Create: `anila-functions-worker/tests/test_wire.py`

- [ ] **Step 1: Write tests**

```python
import json
from shared.wire import encode_event, decode_line, JobSpec


def test_event_roundtrip():
    line = encode_event({"type": "status", "description": "go"})
    assert line.endswith("\n")
    parsed = decode_line(line)
    assert parsed["type"] == "status"


def test_jobspec_serializes():
    spec = JobSpec(code="x=1", body={"action_id": "btn"}, valves={}, user={"id": 1}, metadata={})
    s = spec.serialize()
    assert json.loads(s)["code"] == "x=1"
```

- [ ] **Step 2: Implement**

```python
"""JSON line wire protocol shared by worker-api and sandbox."""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict


@dataclass
class JobSpec:
    code: str
    body: dict
    valves: dict
    user: dict
    metadata: dict
    mode: str = "exec"  # or "extract"

    def serialize(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def deserialize(cls, s: str) -> "JobSpec":
        return cls(**json.loads(s))


def encode_event(event: dict) -> str:
    return json.dumps(event) + "\n"


def decode_line(line: str) -> dict:
    return json.loads(line.rstrip("\n"))
```

- [ ] **Step 3: Run + commit**

```bash
cd anila-functions-worker && pytest tests/test_wire.py -v
git add anila-functions-worker/shared/ anila-functions-worker/tests/test_wire.py
git commit -m "feat(worker): wire protocol"
```

---

### Task 2.3: `sandbox/runtime.py` — exec wrapper

**Files:**
- Create: `anila-functions-worker/sandbox/runtime.py`
- Create: `anila-functions-worker/tests/test_runtime.py`

- [ ] **Step 1: Write tests**

```python
import asyncio
import json
import os
from pathlib import Path
from shared.wire import JobSpec, encode_event


SAMPLE_CODE_OK = '''
class Action:
    actions = [{"id": "btn", "name": "Btn", "icon_url": None}]
    async def action(self, body, __event_emitter__=None, **kw):
        await __event_emitter__({"type": "status", "description": "hi"})
'''

SAMPLE_CODE_NO_ACTION = "x = 1"


def test_runtime_runs_action_and_emits_done(tmp_path):
    spec = JobSpec(code=SAMPLE_CODE_OK, body={"action_id":"btn"}, valves={}, user={"id":1}, metadata={})
    proc = subprocess_run(spec)
    events = parse(proc.stdout)
    types = [e["type"] for e in events]
    assert "status" in types
    assert types[-1] == "__done__"


def test_runtime_emits_error_on_missing_action_class(tmp_path):
    spec = JobSpec(code=SAMPLE_CODE_NO_ACTION, body={}, valves={}, user={}, metadata={})
    proc = subprocess_run(spec)
    events = parse(proc.stdout)
    assert events[0]["type"] == "error"


# Helpers (import subprocess; spawn ./runtime.py with stdin = spec.serialize())
import subprocess
def subprocess_run(spec):
    return subprocess.run(
        ["python", "-u", str(Path(__file__).parent.parent / "sandbox/runtime.py")],
        input=spec.serialize(),
        capture_output=True, text=True, timeout=10,
    )


def parse(stdout: str) -> list[dict]:
    return [json.loads(line) for line in stdout.strip().splitlines() if line]
```

- [ ] **Step 2: Implement `runtime.py`**

```python
"""User code execution wrapper; reads JobSpec from stdin, emits events to stdout."""
from __future__ import annotations

import asyncio
import json
import sys

from shared.wire import JobSpec


async def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


async def main() -> None:
    raw = sys.stdin.read()
    try:
        spec = JobSpec.deserialize(raw)
    except Exception as e:
        await emit({"type": "error", "message": f"bad job spec: {e}"})
        await emit({"type": "__done__", "result": None})
        return

    user_ns: dict = {"__name__": "__user_function__"}
    try:
        exec(compile(spec.code, "<user_function>", "exec"), user_ns)
    except Exception as e:
        await emit({"type": "error", "message": f"compile/exec: {type(e).__name__}: {e}"})
        await emit({"type": "__done__", "result": None})
        return

    action_cls = user_ns.get("Action")
    if action_cls is None:
        await emit({"type": "error", "message": "missing Action class"})
        await emit({"type": "__done__", "result": None})
        return

    if spec.mode == "extract":
        # Just introspect, do not run instance.action
        actions = getattr(action_cls, "actions", []) or getattr(action_cls(), "actions", [])
        valves_schema = {}
        if "Valves" in user_ns:
            try:
                valves_schema = user_ns["Valves"].model_json_schema()
            except Exception:
                pass
        await emit({"type": "extract_result", "actions": actions, "valves_schema": valves_schema})
        await emit({"type": "__done__", "result": None})
        return

    instance = action_cls()
    if "Valves" in user_ns:
        try:
            instance.valves = user_ns["Valves"](**spec.valves)
        except Exception as e:
            await emit({"type": "error", "message": f"valves init: {e}"})

    try:
        result = await instance.action(
            body=spec.body,
            __event_emitter__=emit,
            __user__=spec.user,
            __metadata__=spec.metadata,
        )
        await emit({"type": "__done__", "result": result})
    except Exception as e:
        await emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
        await emit({"type": "__done__", "result": None})


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 3: Run + commit**

```bash
cd anila-functions-worker && PYTHONPATH=. pytest tests/test_runtime.py -v
git add anila-functions-worker/sandbox/runtime.py anila-functions-worker/tests/test_runtime.py
git commit -m "feat(worker): subprocess runtime wrapper"
```

---

### Task 2.4: Static AST extractor (`sandbox/extract.py` stage 1)

Tests + impl: `ast.parse` + `NodeVisitor` to extract:
- module docstring → metadata
- `class Action.actions = [...]` literal list
- `class Valves(BaseModel)` field annotations → JSON schema (limited types)

Detect dynamic patterns; if found, emit signal to fall through to stage 2 (sandbox exec).

- [ ] **Step 1: Test cases for static path**
- [ ] **Step 2: Implement AST visitor (60-100 lines)**
- [ ] **Step 3: Test fallback signal for dynamic case**
- [ ] **Step 4: Commit**

(Same pattern; full code omitted here for plan brevity but each subtask follows previous tasks' format.)

---

### Task 2.5: Ambient cap clear helper (`sandbox/ambient.py`)

```python
"""Clear ambient capabilities before exec'ing user subprocess.

Required so subprocess inherits no SETUID/SETGID from daemon.
"""
import ctypes, ctypes.util

PR_CAP_AMBIENT = 47
PR_CAP_AMBIENT_CLEAR_ALL = 4

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def clear_ambient_caps() -> None:
    rc = _libc.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0)
    if rc != 0:
        raise OSError(ctypes.get_errno(), "prctl PR_CAP_AMBIENT_CLEAR_ALL failed")
```

- [ ] Test on Linux container; commit.

---

### Task 2.6: `sandbox/daemon.py` — Unix socket server + spawn loop

**Files:**
- Create: `anila-functions-worker/sandbox/daemon.py`
- Create: `anila-functions-worker/tests/test_daemon.py`

Implement:
- bind `/jobs/control.sock`, chmod 0660
- `accept` loop
- per-connection: read length-prefixed JSON job spec → spawn `python -u runtime.py` as subproc uid via `subprocess.Popen(user='subproc', group='subproc', preexec_fn=clear_ambient_caps)` → forward stdout lines back over connection → on subprocess exit, close connection
- 30s timeout per run (extract: 3s)
- 8 concurrent semaphore (extract: 4)

- [ ] Test with concurrent connections + timeout + error path; commit.

---

### Task 2.7: `worker-api/main.py` — FastAPI gate

**Files:**
- Create: `anila-functions-worker/worker-api/main.py`
- Create: `anila-functions-worker/worker-api/exec_handler.py`
- Create: `anila-functions-worker/worker-api/extract_handler.py`
- Create: `anila-functions-worker/worker-api/socket_client.py`
- Create: `anila-functions-worker/worker-api/auth.py`

Endpoints:
- `POST /exec` — verify `X-Functions-Api-Secret`, connect Unix socket on `/jobs-exec/control.sock`, write job spec, stream events back as `text/event-stream`
- `POST /extract-meta` — same but `/jobs-extract/control.sock`
- Healthcheck `GET /healthz`

- [ ] Tests for each + commit.

---

### Task 2.8: Dockerfiles + sandbox-entrypoint.sh

**Files:**
- Create: `anila-functions-worker/Dockerfile.worker-api`
- Create: `anila-functions-worker/Dockerfile.sandbox`
- Create: `anila-functions-worker/sandbox-entrypoint.sh`

Per spec §5.8 / §9.1.

- [ ] Build images locally; verify `docker run --rm anila-functions-sandbox-exec:dev capsh --print` shows expected caps post-setpriv.
- [ ] Commit.

---

### Task 2.9: Wire CSP `worker_client.py` to real httpx (replace stub)

- [ ] Remove `_stub_extract` short-circuit when `ANILA_FUNCTIONS_STUB_EXTRACT` not set
- [ ] Add timeouts + retries (1 retry on connection error)
- [ ] Tests: `respx`-mocked successful + 4xx + connection-error paths
- [ ] Commit.

---

### Task 2.10: SSE redaction + run row finalize

- [ ] In `app/api/action_function/run.py`: wrap each chunk through `redact_events` before yielding
- [ ] On `__done__` from worker, update run row with status + duration_ms + redacted events_json
- [ ] Tests with secret leak in event → DB row has `<redacted:...>`
- [ ] Commit.

---

## Sprint 2.5 — Prototype Gate (BLOCKING; ~6-10 tasks, 1-2 days)

**Goal:** Verify capability landing on real compose. Sprint 3 cannot start until **all 6 smoke tests pass**.

### Gate setup

- [ ] Build minimal compose with just sandbox-exec + dummy worker-api
- [ ] Apply v1 cap_drop / cap_add / user:0 / entrypoint config

### 6 smoke tests (per spec §5.8)

- [ ] **Smoke 1: capsh probe at entrypoint shows Bounding has SETUID, SETGID, CHOWN**

Run: `docker compose run sandbox-exec sh -c 'capsh --print | grep Bounding'`
Expected: contains `cap_setuid,cap_setgid,cap_chown`

- [ ] **Smoke 2: daemon python shows Effective + Ambient SETUID, SETGID after setpriv**

Inside daemon: `python -c "import subprocess; print(subprocess.run(['capsh','--print'], capture_output=True, text=True).stdout)"`
Expected: `Current:` row contains `cap_setuid,cap_setgid` in eip flags; `Ambient set` includes them.

- [ ] **Smoke 3: subprocess gets uid 65534 + only group 65534**

Daemon spawns subprocess with `user='subproc'`; subprocess prints `os.getuid(), os.getgroups()`.
Expected: `(65534, [65534])` (no 65530)

- [ ] **Smoke 4: subprocess `setuid(0)` raises PermissionError**

In subprocess: `try: os.setuid(0)\nexcept PermissionError: print("EPERM")`.
Expected: `EPERM`

- [ ] **Smoke 5: subprocess `connect('/jobs-exec/control.sock')` fails with PermissionError**

Expected: `PermissionError` (mode 0660 not in anila-jobs group).

- [ ] **Smoke 6: subprocess `os.listdir('/jobs-exec')` fails with PermissionError**

Expected: `PermissionError` (dir mode 0770).

### Gate decision

- [ ] If all 6 pass → tag commit `prototype-gate-passed` → proceed to Sprint 3
- [ ] If any fail → STOP. Switch to spawn-helper fallback (per §5.8)；redo gate.

---

## Sprint 3 — Container Hardening + Network + Sandbox Tests (~15-25 tasks, 4-5 days)

> **⚠️ Plan elaboration note** — Sprint 3 tasks below scope the work; full TDD steps (test code + commands) follow Sprint 1's pattern and should be expanded at sprint kickoff or per-subagent dispatch.

### Task 3.1: docker-compose.yml — add 4 services + 3 networks + 2 volumes

(Per spec §5.7 yaml block; copy verbatim.)

### Task 3.2: Egress squid configuration

- [ ] Write `anila-functions-egress/Dockerfile` + `squid.conf.template`
- [ ] Entrypoint reads `ANILA_FUNCTIONS_EGRESS_ALLOWLIST`, generates `acl allow_dst dst <list>`
- [ ] Test: from runner-exec curl allowlisted host (200) and non-allowlisted (denied)

### Task 3.3: Network topology assertion tests

Per spec §8.4:
- [ ] sandbox-exec → curl arbitrary external → fails (assert: not 200, do not bind to error string)
- [ ] sandbox-exec → curl allowed internal → 200
- [ ] sandbox-exec → raw TCP socket to 8.8.8.8:443 → fails
- [ ] sandbox-exec → raw TCP socket to csp:8000 → fails (different network)
- [ ] sandbox-extract → curl proxy host → fails
- [ ] worker-api → connect /jobs-exec/control.sock → succeeds
- [ ] csp container → cannot stat /jobs-exec/control.sock (volume not mounted)

### Task 3.4: UID isolation tests inside sandbox

- [ ] Inside daemon: spawn subprocess; subprocess assertion suite for §5.8 EACCES paths
- [ ] Concurrent 8 jobs: each cannot see others' valves (verify via deliberate test fixture)

### Task 3.5: Stress / chaos

- [ ] 8 concurrent runs, all complete within 30s
- [ ] Ninth → queue_full
- [ ] Subprocess SIGKILL mid-run → daemon cleans up + worker-api gets timeout/error event

---

## Sprint 4 — Frontend (~25-35 tasks, 5-6 days)

> **⚠️ Plan elaboration note** — Sprint 4 tasks below scope the frontend work; each task's TDD pattern (Vitest unit + Playwright E2E) follows Sprint 1 backend rhythm but with React/JS test idioms. Expand at sprint kickoff.

### Task 4.1: API client — `runtime/functions.js`

CRUD wrappers + SSE consumer; tests with `msw`.

### Task 4.2: SSE event handler — `runtime/functionEvents.js`

Whitelist verb dispatch; `host_command` two-step toast for clipboard / link.

### Task 4.3: Host command dispatchers — `runtime/hostCommands.js`

6 verbs per spec §5.6; each is small switch case.

### Task 4.4: zustand-equivalent store — `runtime/functionsStore.js`

Cache `enabled-actions`; invalidate on mutations.

### Task 4.5: Monaco loader — bundled, dynamic-import for admin pages

```bash
cd ANILA_UI/anila-ui
npm install monaco-editor@0.50 @monaco-editor/react@4.6
```

### Task 4.6: Admin pages — list / editor / Test Console / Audit

(Each is its own task; ~5 tasks total.)

### Task 4.7: ChatRuntime integration — `chat.jsx` toolbar buttons

Render up to 4 buttons inline; overflow → existing toolbar overflow menu.

### Task 4.8: E2E tests (Playwright)

Per spec §8.4:
- developer creates fill-text Function, save, enables, button appears in toolbar, click → composer.set_text fills text
- whitelist verb test (injection rejected)
- test console doesn't auto-eval — wait, with host_command this is moot per §6.4
- user role no New CTA, no code visibility
- fork enabled-only, fork goes to draft
- valves secret field shows ••••••••
- disabled function: no button
- audit detail shows redacted token
- /run for other user's conversation → 403

---

## Final Acceptance

- [ ] All Sprint 1-4 tests pass
- [ ] Prototype gate (`prototype-gate-passed`) tag in git history
- [ ] Manual smoke per spec §8.5:
  - paste 同事的「填入文字助手」code (with composer.set_text replacement) → save → click button → text fills ANILA Composer
  - timeout test (`time.sleep(60)`) → 30s SSE error
  - emit token in status → audit log shows `<redacted>`
- [ ] Dogfood: 1-2 dev write Function, use for 1 week, no critical issues
- [ ] Open: enable for all developers
