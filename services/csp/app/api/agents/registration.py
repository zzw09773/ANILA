"""Agent registration / CRUD / template download endpoints.

Split verbatim from the former single-module ``app/api/agents.py``
(behavior-preserving refactor).
"""
# ``io`` / ``zipfile`` are unused here since the zip writing moved into
# ``_quickstart_bundle``, but ``app.api.agents`` re-exports them from this
# module for import/monkeypatch compatibility — so they stay.
import io  # noqa: F401
import zipfile  # noqa: F401
from datetime import datetime
from pathlib import Path

import anila_core
from anila_core.security import UnsafeEndpointError, validate_outbound_url
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session
from app.api.agents import _quickstart_bundle as _bundle
from app.database import get_db
from app.models.agent import Agent
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.services.auth_service import (
    get_current_user,
    is_admin_tier,
    require_admin,
)

from app.api.agents._common import (
    _client_ip,
    _require_agent_editor,
    _require_developer_or_admin,
    apply_default_classification_level,
    ensure_agent_view_access,
    parse_stored_classification_level,
    refuse_bind_above_agent_level,
    refuse_classification_downgrade,
    requires_controlled_access,
    validate_agent_manifest,
)
from app.schemas.contracts.agents import (
    REGISTER_DEFAULT_APPROVAL,
    ApprovalStatus,
    RuntimeType,
    validate_description_for_router,
)
from app.schemas.contracts.classification import ClassificationLevel
from app.services.agent_collection_bindings import (
    get_bound_collection_ids,
    resolve_requested_collection_ids,
    set_bound_collection_ids,
)
from app.schemas.base import ApiResponseModel


def _enforce_endpoint_url(url: str) -> None:
    """Reject SSRF-prone agent endpoint URLs (loopback / private / metadata).

    ``anila_core.security.url_guard.validate_outbound_url`` is the same
    helper the ingestion-credentials API uses; agents now share the
    deny-list so a developer can't register an internal-only endpoint and
    have an admin unknowingly approve it.

    Slice 6a (doc 04 §8): validated with ``endpoint_kind="agent"`` — http
    agent endpoints are allowed via ``ANILA_ALLOW_HTTP_AGENT_ENDPOINT``
    (legacy ``ANILA_ALLOW_HTTP_ENDPOINT`` still works with a deprecation
    warning so intranet MLSteam http NodePort agents keep registering).
    """
    try:
        validate_outbound_url(url, endpoint_kind="agent")
    except UnsafeEndpointError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

def _repo_root() -> Path | None:
    """``<repo-root>`` from services/csp/app/api/agents/registration.py.

    Six levels: agents → api → app → csp → services → repo root. Returns
    None when the walk does not land on a tree that looks like this repo —
    inside the csp image the parents bottom out at ``/``. Returning None
    rather than raising keeps that case a 404 on one route; raising here
    runs at import time and would stop the whole service from starting.
    """
    root = Path(__file__).resolve().parent.parent.parent.parent.parent.parent
    if not (root / "services" / "csp").is_dir():
        return None
    return root


def _default_template_dir() -> Path:
    """Locate the bundled agent template without a runtime path knob.

    The tree used to keep the agent template at ``<repo-root>/anila-agent``;
    the §17.1 directory move (b5c5e32e, a pure ``git mv``) relocated it to
    ``packages/anila-agent``. The image mount is fixed at
    ``/app/anila-template``; a source checkout uses the repository package path.
    """
    root = _repo_root()
    # No repo layout (the csp image): return a path that cannot exist, so the
    # download route answers 404 instead of the service failing to import.
    return (root / "packages" / "anila-agent") if root else Path("/nonexistent/anila-agent")


_TEMPLATE_DIR = Path("/app/anila-template")
if not _TEMPLATE_DIR.exists():
    _TEMPLATE_DIR = _default_template_dir()


def _default_quickstart_dir() -> Path:
    """Repo-side fallback for the quickstart scaffold (compose mounts it too)."""
    root = _repo_root()
    return (
        (root / "packages" / "anila-agent-quickstart")
        if root
        else Path("/nonexistent/anila-agent-quickstart")
    )


# Bundle inputs, resolved once at import (like ``_TEMPLATE_DIR``) and passed
# explicitly to the builder. Env names live in ``_quickstart_bundle``. The
# scaffold has a repo-checkout fallback so a bare uvicorn run works; the
# profile stays ops-supplied with no fallback — it is a release input with
# no in-repo default. Wheels are installed into the MLSteam lab image and
# are not a download input (design §12).
_QUICKSTART_DIR = _bundle.quickstart_source_dir()
if not _QUICKSTART_DIR.is_dir():
    _QUICKSTART_DIR = _default_quickstart_dir()

_QUICKSTART_PROFILE = _bundle.profile_path()

# Public trust anchors. Only their bytes are used, and they are parsed — a path
# that exists is not proof that it holds a usable chain (design §6).
_PLATFORM_CA_BUNDLE = (
    Path(__file__).resolve().parent.parent.parent / "services" / "cspki_ca_bundle.pem"
)

def _resolve_anila_verify_source() -> Path:
    """Standalone JWT verifier path from the *installed* ``anila_core`` package.

    Hatchling ships the whole ``src/anila_core`` tree (incl. ``contrib/``) in
    the wheel. Resolve via ``anila_core.__file__`` — NOT
    ``_repo_root()/packages/...``, which bottoms out at ``/`` in the csp
    image and can never exist there. Absent until that package lands → 503.
    """
    return (
        Path(anila_core.__file__).resolve().parent / "contrib" / "anila_verify.py"
    )


_ANILA_VERIFY_SOURCE = _resolve_anila_verify_source()

router = APIRouter()

# Aliased to the bundle's single rule set so the two cannot drift: the advanced
# example is the only consumer of this filter (the quickstart uses a fixed
# 11-file allow-list). ``pem``/``key`` are dropped because the example ships
# ``.env.example`` instead of a real ``.env``, and no private material belongs
# in the zip.
_IGNORED_TEMPLATE_PARTS = _bundle._ADVANCED_EXCLUDED_PARTS
_IGNORED_TEMPLATE_SUFFIXES = _bundle._ADVANCED_EXCLUDED_SUFFIXES


# ── Schemas ──────────────────────────────────────────────────────────────────

_DEAD_CAPABILITIES_MSG = (
    "capabilities 已退場:執行路徑不閘控此欄位,請勿再傳送"
)
_DEAD_AGENT_CEILING_MSG = (
    "classification_ceiling 已退場:agent 請用 default_classification_level;"
    "模型端 ceiling 才有寫入與 enforcement"
)


def _reject_dead_agent_controls(data: object) -> object:
    """Refuse vestigial fields that were accepted then discarded."""
    if not isinstance(data, dict):
        return data
    if data.get("classification_ceiling") is not None:
        raise ValueError(_DEAD_AGENT_CEILING_MSG)
    if data.get("capabilities") is not None:
        raise ValueError(_DEAD_CAPABILITIES_MSG)
    # Drop explicit nulls so they cannot look like a successful clear.
    data.pop("classification_ceiling", None)
    data.pop("capabilities", None)
    return data


class AgentRegisterRequest(BaseModel):
    # Undeclared fields are refused, not swallowed. Pydantic's default
    # extra="ignore" is what let the CLI's ``draft: true`` be accepted and
    # thrown away — the developer got "✓ registered" for a shadow agent
    # that was never shadow. Both callers (anila-core CLI + governance UI)
    # now send only declared fields, so a 422 here means a genuine
    # client/server drift rather than a working feature being blocked.
    model_config = {"extra": "forbid"}

    name: str
    endpoint_url: str
    description_for_router: str
    api_version: str = "v1"
    # Every registered agent must declare the base LLM / VLM it wraps so
    # usage metering can attribute tokens to a real model_registry row.
    # Without this the dashboard's per-model breakdown has phantom
    # "agent X" traffic with no underlying model behind it.
    #
    # Either representation satisfies that: the numeric id (what the
    # governance UI has, because it renders a <select> of models) or the
    # model NAME (what a developer has, because ``anila.yaml`` writes
    # ``base_model: "<name>"``). Requiring the id from the CLI meant the
    # documented registration command could not succeed at all — the CLI
    # only ever sends a name — and forced every developer to look a
    # numeric database id up in the governance UI first.
    base_model_id: int | None = Field(
        default=None, description="底層模型 ID(與 base_model_name 擇一即可)"
    )
    base_model_name: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "底層模型名稱(model_registry.name,或治理中心顯示名稱);"
            "與 base_model_id 擇一即可"
        ),
    )
    # RAG agents: collections this agent may search via dispatch JWT (P4.7 / S-Q1).
    # Prefer ``collection_ids`` (zero / one / several). Legacy ``collection_id``
    # is still accepted and expanded to a one-element set when the list is
    # omitted — one representation is derived from the other, never both
    # written independently.
    collection_ids: list[int] | None = Field(
        default=None,
        description="RAG agent 綁定的知識庫 id 清單（可多個；空清單＝未綁定）",
    )
    collection_id: int | None = Field(
        default=None,
        description="（相容）單一知識庫 id；未送 collection_ids 時展開為單元素集合",
    )
    input_schema: dict | None = None
    # 派工只認 OpenAI 相容 /v1/chat/completions；治理 UI 不再讓人選型別。
    # 欄位保留給 CLI／舊呼叫，未送時寫預設。
    runtime_type: RuntimeType = RuntimeType.OPENAI_COMPATIBLE_AGENT
    # doc 05 §3 agent semver。**欄位名以資料庫欄位為準**(models/agent.py:83
    # ``agent_version``),回應也是這個名字(AgentResponse.agent_version)。
    # 線上仍接受舊拼法 ``version``:治理 UI 與 anila-core CLI 都送過這個
    # 名字,而 BaseModel 預設 extra="ignore" 會把它「收下然後丟掉」——
    # 開發者填了版本、沒有錯誤、詳情頁永遠顯示「—」。別名讓舊送法真的
    # 存進去,而不是在兩端各自沉默。manifest 的欄位名也是 ``version``
    # (schemas/contracts/agents.py:130),對映同一欄。
    agent_version: str | None = Field(
        default=None,
        max_length=40,
        validation_alias=AliasChoices("agent_version", "version"),
        description="agent 版本字串(例:1.0.0);未提供時取 manifest.version",
    )
    # doc 05 §4 optional manifest —— 提供則 fail-closed 驗證(422)並留存 manifest_json。
    manifest: dict | None = None
    # OE-1: shadow/draft 已退場。欄位保留為相容(忽略),一律落地 registered。
    shadow: bool = False
    # G9: developer chooses the project's classification level at register time.
    # Stored in ``default_classification_level``; ``requires_encryption`` is
    # derived (level >= 密). Unknown values → 422 via ClassificationLevel.
    default_classification_level: ClassificationLevel = ClassificationLevel.UNCLASSIFIED

    @field_validator("description_for_router")
    @classmethod
    def _check_description_for_router(cls, value: str) -> str:
        checked = validate_description_for_router(value)
        if checked is None:
            raise ValueError("description_for_router 不可省略。")
        return checked

    @model_validator(mode="before")
    @classmethod
    def _reject_dead_controls(cls, data: object) -> object:
        return _reject_dead_agent_controls(data)

    @model_validator(mode="after")
    def _require_a_base_model(self) -> "AgentRegisterRequest":
        """One of the two base-model fields must be present (either shape)."""
        if self.base_model_id is None and not (self.base_model_name or "").strip():
            raise ValueError(
                "必須指定底層模型:填 base_model_name(模型名稱)或 base_model_id(數字 id)"
                "其中之一。anila.yaml 請填 base_model:「模型名稱」。"
            )
        return self


class AgentResponse(ApiResponseModel):
    id: int
    name: str
    owner_user_id: int
    owner_username: str | None = None
    endpoint_url: str
    api_version: str
    description_for_router: str
    base_model_id: int | None = None
    base_model_name: str | None = None
    # Derived mirror (min of bound_collection_ids, or null). Kept for
    # legacy callers; prefer bound_collection_ids for multi-bind.
    bound_collection_id: int | None = None
    bound_collection_ids: list[int] = Field(default_factory=list)
    health_status: str
    approval_status: str
    # 底層模型下線等原因。NULL 表示不因此不可用。與健康、核准無關。
    unavailable_reason: str | None = None
    requires_encryption: bool = False
    # doc 05 §3/§4/§6 registry-upgrade fields (Slice 5a). Optional so existing
    # consumers keep working; surfaced for the developer/admin registry UI.
    runtime_type: str | None = None
    agent_version: str | None = None
    audit_level: str | None = None
    default_classification_level: str | None = None
    manifest_json: dict | None = None
    # Sprint 13 PR A3 — admin-editable runtime knobs (tool permissions,
    # workspace caps, guardrails). NULL means "agent uses code defaults".
    # Writes retired (official agent never polls); field kept for read-only.
    runtime_config: dict | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# Agents and Models track health status in different vocabularies —
# Agent uses "healthy"/"unhealthy"/"unknown", ModelRegistry uses
# "online"/"offline"/"connecting". The background health_checker writes
# the ModelRegistry vocabulary into Agent rows, so both sets can coexist
# in the DB. Normalize on read so the admin UI sees a single vocabulary
# and "HEALTHY" count in the dashboard matches the table rows.
_AGENT_HEALTH_MAP = {
    "online": "healthy",
    "healthy": "healthy",
    "connecting": "unknown",
    "unknown": "unknown",
    "offline": "unhealthy",
    "unhealthy": "unhealthy",
}


def _serialize_agent(agent: Agent) -> dict:
    raw = agent.health_status or "unknown"
    normalized = _AGENT_HEALTH_MAP.get(raw, raw)
    owner = getattr(agent, "owner", None)
    base = getattr(agent, "base_model", None)
    # Mirror is derived from the set so a drifted column cannot
    # contradict bound_collection_ids in the response.
    bound_ids = get_bound_collection_ids(agent)
    return {
        "id": agent.id,
        "name": agent.name,
        "owner_user_id": agent.owner_user_id,
        "owner_username": owner.username if owner else None,
        "endpoint_url": agent.endpoint_url,
        "api_version": agent.api_version,
        "description_for_router": agent.description_for_router,
        "base_model_id": agent.base_model_id,
        "base_model_name": base.display_name if base else None,
        "bound_collection_ids": bound_ids,
        "bound_collection_id": bound_ids[0] if bound_ids else None,
        "health_status": normalized,
        "approval_status": agent.approval_status,
        "unavailable_reason": getattr(agent, "unavailable_reason", None),
        "requires_encryption": bool(getattr(agent, "requires_encryption", False)),
        "runtime_type": getattr(agent, "runtime_type", None),
        "agent_version": getattr(agent, "agent_version", None),
        "audit_level": getattr(agent, "audit_level", None),
        "default_classification_level": getattr(
            agent, "default_classification_level", None
        ),
        "manifest_json": getattr(agent, "manifest_json", None),
        "runtime_config": getattr(agent, "runtime_config", None),
        "created_at": agent.created_at,
    }


class AgentUpdateRequest(BaseModel):
    """Owner / admin-editable fields. Intentionally omits:
    - ``name`` (agent_id referenced by every registered client — immutable)
    - ``approval_status`` (dedicated /approve + /reject admin endpoints)
    - ``owner_user_id`` (transfer of ownership isn't exposed yet)
    - ``capabilities`` / ``classification_ceiling`` (accepted-then-discarded;
      refuse rather than store a setting that looks enforced)

    ``default_classification_level`` may also be set via the dedicated
    ``POST /api/agents/{id}/classification`` endpoint; both paths derive
    ``requires_encryption`` from the level (mirror threshold ≥ 密).
    """
    endpoint_url: str | None = None
    api_version: str | None = None
    description_for_router: str | None = None
    base_model_id: int | None = None
    input_schema: dict | None = None
    # doc 05 §4 — replace the stored manifest snapshot (validated fail-closed).
    manifest: dict | None = None
    default_classification_level: ClassificationLevel | None = None
    # P4.7 — replace the bound collection set. Same derivation rule as
    # register: ``collection_ids`` wins; else expand legacy ``collection_id``.
    # Omit both to leave bindings unchanged. Empty list clears all bindings.
    collection_ids: list[int] | None = None
    collection_id: int | None = None

    @field_validator("description_for_router")
    @classmethod
    def _check_description_for_router(cls, value: str | None) -> str | None:
        # None ＝ 不改此欄；送了空字串則 422。
        return validate_description_for_router(value, allow_unset=True)

    @model_validator(mode="before")
    @classmethod
    def _reject_dead_controls(cls, data: object) -> object:
        return _reject_dead_agent_controls(data)


def _active_model_names(db: Session, limit: int = 12) -> str:
    """Comma-joined names of active models, for "what do I type instead?" hints."""
    from app.models.model_registry import ModelRegistry

    rows = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.is_active.is_(True))
        .order_by(ModelRegistry.name)
        .limit(limit)
        .all()
    )
    if not rows:
        return "(目前沒有已啟用的模型,請聯絡平台管理員)"
    return "、".join(r.name for r in rows)


def _lookup_model_by_name(db: Session, raw_name: str):
    """Resolve a model NAME to its ``model_registry`` row.

    ``model_registry.name`` is unique, so an exact hit on it is
    authoritative and can never be ambiguous. Only the human-facing
    ``display_name`` can collide, and when it does we say which rows
    collided instead of silently picking one.
    """
    from app.models.model_registry import ModelRegistry

    name = raw_name.strip()
    exact = db.query(ModelRegistry).filter(ModelRegistry.name == name).first()
    if exact is not None:
        return exact

    matches = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.display_name == name)
        .order_by(ModelRegistry.id)
        .all()
    )
    if not matches:
        raise HTTPException(
            status_code=400,
            detail=(
                f"底層模型「{name}」不存在。目前可用的模型名稱:"
                f"{_active_model_names(db)};"
                "請把 anila.yaml 的 base_model 改成其中之一,或改填 base_model_id。"
            ),
        )
    if len(matches) > 1:
        listed = "、".join(f"{m.name}(id={m.id})" for m in matches)
        raise HTTPException(
            status_code=400,
            detail=(
                f"底層模型名稱「{name}」對應到多個模型:{listed}。"
                "請把 base_model 改成上列其中一個的完整名稱,或直接指定 base_model_id。"
            ),
        )
    return matches[0]


def _resolve_base_model(
    db: Session, *, base_model_id: int | None, base_model_name: str | None
):
    """Resolve the agent's base model from an id, a name, or both.

    Both are allowed for callers in transition, but they must agree —
    accepting a contradictory pair and quietly honouring one of them is
    the same silent-discard failure this endpoint is being fixed for.
    """
    from app.models.model_registry import ModelRegistry

    base = None
    if base_model_id is not None:
        base = (
            db.query(ModelRegistry)
            .filter(ModelRegistry.id == base_model_id)
            .first()
        )
        if base is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"底層模型 id={base_model_id} 不存在。"
                    f"目前可用的模型名稱:{_active_model_names(db)};"
                    "也可以改填 base_model_name(模型名稱)免去查 id。"
                ),
            )

    if base_model_name and base_model_name.strip():
        by_name = _lookup_model_by_name(db, base_model_name)
        if base is None:
            base = by_name
        elif by_name.id != base.id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"base_model_id={base.id}(「{base.name}」)與 "
                    f"base_model_name「{base_model_name}」(id={by_name.id})"
                    "指向不同模型。請只填其中一個。"
                ),
            )

    assert base is not None  # guaranteed by AgentRegisterRequest validation
    if not base.is_active:
        raise HTTPException(
            status_code=400,
            detail=(
                f"底層模型「{base.display_name}」已停用,無法用來註冊 agent。"
                f"請改用已啟用的模型({_active_model_names(db)}),"
                "或請平台管理員重新啟用這個模型。"
            ),
        )
    return base


def _validate_collection_access_for_ids(
    db: Session, user: User, collection_ids: list[int]
) -> None:
    """Every id must be usable by ``user`` under ``_require_collection_access``.

    Bind and search ask about the same subject: the agent runs as its
    owner (dispatch-JWT search principal = owner), so entitlement is the
    owner's — not the editor's. An admin may edit an agent but may only
    bind collections that owner could themselves search; otherwise the
    binding is inert at search time and bricks the owner's console save
    (which always resends the full set).
    """
    from app.api.ingestion.collections import _require_collection_access

    for cid in collection_ids:
        _require_collection_access(db, user, cid)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _should_include_template_path(path: Path, root: Path) -> bool:
    """Kept as a predicate over the bundle's single exclusion rule set."""
    return not _bundle._skip_advanced(path.relative_to(root), path)


# ── Endpoints ─────────────────────────────────────────────────────────────────

# ── Download bundles (design docs/designs/agent-quickstart-scaffold-2026-09-22) ─

def _bundle_error(exc: _bundle.BundleError) -> HTTPException:
    """Assembly gaps are 503 — never a partial zip, never a silent fallback."""
    return HTTPException(status_code=503, detail=exc.detail)


def _zip_response(bundle: _bundle.BuiltBundle, background: BackgroundTasks) -> FileResponse:
    """Stream the assembled temp file, removing it once the response is done."""
    background.add_task(bundle.path.unlink, missing_ok=True)
    return FileResponse(
        bundle.path,
        media_type="application/zip",
        filename=bundle.filename,
    )


def _load_download_agent(db: Session, current_user: User, agent_id: int) -> Agent:
    """Resolve ``agent_id`` and gate it with the standard view predicate.

    Same 404 as ``GET /api/agents/{id}`` so a foreign id is not an oracle.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    ensure_agent_view_access(agent, current_user)
    return agent


@router.get("/template/download")
def download_quickstart_bundle(
    background: BackgroundTasks,
    agent_id: int | None = Query(
        default=None,
        description=(
            "選填。不帶時下載通用包：不含 agent id、不含模型名稱。"
            "帶已註冊 agent 的 id 時預填 ANILA_AGENT_ID 與底層模型名稱，並驗 owner/admin。"
        ),
    ),
    collection_id: int | None = Query(
        default=None,
        description="選填；必須在該 agent 已綁定的 collection 集合內，只預填這一個",
    ),
    current_user: User = Depends(_require_developer_or_admin),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Default download: the quickstart scaffold, pre-wired and directly deployable.

    Redefines the old ``/template/download`` (zero external users, so no
    compat alias — a download that does not say which project it is was the
    bug). The advanced example moved to ``/examples/advanced/download``.

    Everything unverifiable is fail-closed: missing scaffold file, missing
    site profile, a lock that is not a complete hash lock, or a missing lab
    image version yields 503 rather than a zip that only looks runnable.
    The zip does not contain a wheelhouse.
    """
    agent: Agent | None = None
    if agent_id is not None:
        agent = _load_download_agent(db, current_user, agent_id)
    elif collection_id is not None:
        raise HTTPException(
            status_code=400,
            detail="collection_id 必須搭配 agent_id 使用（綁定集合存在 agent 上）",
        )

    if agent is not None and collection_id is not None:
        bound = get_bound_collection_ids(agent)
        if collection_id not in bound:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"collection_id={collection_id} 不在 agent「{agent.name}」"
                    f"已綁定的集合 {bound} 內"
                ),
            )

    try:
        bundle = _bundle.build_quickstart_bundle(
            agent_id=agent.id if agent else None,
            agent_name=agent.name if agent else None,
            base_model_id=agent.base_model_id if agent else None,
            base_model_name=(
                getattr(getattr(agent, "base_model", None), "name", None)
                if agent
                else None
            ),
            bound_collection_ids=get_bound_collection_ids(agent) if agent else [],
            requested_collection_id=collection_id,
            verifier_source=_ANILA_VERIFY_SOURCE,
            platform_ca_default=_PLATFORM_CA_BUNDLE,
            profile_file=_QUICKSTART_PROFILE,
            source_dir=_QUICKSTART_DIR,
        )
    except _bundle.BundleError as exc:
        raise _bundle_error(exc) from exc
    return _zip_response(bundle, background)


@router.get("/examples/advanced/download")
def download_advanced_example(
    background: BackgroundTasks,
    current_user: User = Depends(_require_developer_or_admin),
) -> FileResponse:
    """Advanced example download — ``packages/anila-agent`` mirrored as-is.

    Separate endpoint on purpose: the two downloads are independent
    implementations of one platform contract, and each declares its own
    filename/zip root. Auth gate matches the quickstart download.
    """
    try:
        bundle = _bundle.build_advanced_example_bundle(source_dir=_TEMPLATE_DIR)
    except _bundle.BundleError as exc:
        raise _bundle_error(exc) from exc
    return _zip_response(bundle, background)


# The route path stays ``/template/download`` (zero external users, so no
# alias URL), but the old function name is kept: ``app.api.agents`` re-exports
# it for monkeypatch compatibility.
download_template = download_quickstart_bundle


@router.get("/platform-ca/download")
def download_platform_ca(
    current_user: User = Depends(_require_developer_or_admin),
) -> Response:
    """Serve the intranet CSPKI CA bundle for third-party agent HTTPS/JWKS trust.

    Public certificates only (no private keys). Auth matches ``/template/download``.
    """
    ca_path = _PLATFORM_CA_BUNDLE
    if not ca_path.is_file():
        raise HTTPException(
            status_code=503,
            detail="平台 CA 套件在此部署環境中無法取得，請聯絡維運",
        )
    return Response(
        content=ca_path.read_bytes(),
        media_type="application/x-pem-file",
        headers={
            "Content-Disposition": "attachment; filename=anila-platform-ca.pem",
        },
    )


@router.get("/anila-verify/download")
def download_anila_verify(
    current_user: User = Depends(_require_developer_or_admin),
) -> Response:
    """Serve the standalone ``anila_verify.py`` JWT verifier for copy-paste agents.

    Auth matches ``/platform-ca/download``. If the sibling package has not
    shipped the file into this deployment, refuse with 503 — never an empty
    200 or a fabricated stub.
    """
    verify_path = _ANILA_VERIFY_SOURCE
    if not verify_path.is_file():
        raise HTTPException(
            status_code=503,
            detail="JWT 驗證器原始碼在此部署環境中無法取得，請聯絡維運",
        )
    return Response(
        content=verify_path.read_bytes(),
        media_type="text/x-python",
        headers={
            "Content-Disposition": "attachment; filename=anila_verify.py",
        },
    )


@router.post("/register", response_model=AgentResponse)
def register_agent(
    request: AgentRegisterRequest,
    http_request: Request,
    current_user: User = Depends(_require_developer_or_admin),
    db: Session = Depends(get_db),
):
    existing = db.query(Agent).filter(Agent.name == request.name).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Agent 名稱「{request.name}」已存在")

    # SSRF guard — block loopback / private / cloud-metadata endpoints
    # before they ever land in the DB. Same helper the ingestion
    # credentials API uses (anila_core.security.url_guard).
    _enforce_endpoint_url(request.endpoint_url)

    # Validate base model — a registered agent must wrap a real, active
    # model_registry row so per-model usage accounting stays truthful.
    # Accepts either the numeric id (governance UI) or the model name
    # (anila.yaml / anila-core CLI); see ``_resolve_base_model``.
    base = _resolve_base_model(
        db,
        base_model_id=request.base_model_id,
        base_model_name=request.base_model_name,
    )

    # RAG agents: bind zero / one / several collections the agent may search.
    # ``collection_ids`` wins; legacy ``collection_id`` expands to a singleton.
    # Validate the registering owner actually has access to each id.
    fields_set = request.model_fields_set
    requested_ids = resolve_requested_collection_ids(
        collection_ids=request.collection_ids,
        collection_id=request.collection_id,
        collection_ids_provided="collection_ids" in fields_set,
        collection_id_provided="collection_id" in fields_set,
    )
    # Register defaults to unbound when neither field is sent.
    bind_ids = requested_ids if requested_ids is not None else []
    if bind_ids:
        _validate_collection_access_for_ids(db, current_user, bind_ids)

    # doc 05 §4 — optional manifest is validated fail-closed (422) and the
    # normalized snapshot is stored so the registry has the formal schema
    # (not just the loose ``capabilities`` blob).
    manifest_json = (
        validate_agent_manifest(request.manifest)
        if request.manifest is not None
        else None
    )

    # doc 05 §3 — 明送的版本優先,否則沿用 manifest.version。兩者都沒有才是 NULL。
    agent_version = request.agent_version or (
        (manifest_json or {}).get("version") or None
    )

    # OE-1: every register lands as registered (shadow flag ignored).
    level = request.default_classification_level
    agent = Agent(
        name=request.name,
        owner_user_id=current_user.id,
        endpoint_url=request.endpoint_url,
        api_version=request.api_version,
        description_for_router=request.description_for_router,
        base_model_id=base.id,
        bound_collection_id=None,  # set via bindings helper after flush
        capabilities=None,
        input_schema=request.input_schema,
        runtime_type=request.runtime_type.value,
        agent_version=agent_version,
        manifest_json=manifest_json,
        approval_status=REGISTER_DEFAULT_APPROVAL,
        default_classification_level=level.to_storage(),
        requires_encryption=requires_controlled_access(level),
    )
    # P4.9 — agent level must cover every collection being bound. Check
    # against the in-memory agent (not yet flushed) so register refuses
    # before inserting junction rows.
    if bind_ids:
        refuse_bind_above_agent_level(db, agent, bind_ids)
    db.add(agent)
    db.flush()  # need agent.id for junction rows
    set_bound_collection_ids(db, agent, bind_ids)
    db.commit()
    db.refresh(agent)
    log_audit_event(
        db, actor=current_user, action="register", resource_type="agent",
        resource_id=agent.id,
        detail=(
            f"註冊 agent「{agent.name}」,預設分類等級={level.to_storage()}"
        ),
        ip_address=_client_ip(http_request),
        metadata={
            "default_classification_level": level.to_storage(),
            "requires_controlled_access": agent.requires_encryption,
            "bound_collection_ids": get_bound_collection_ids(agent),
        },
        commit=True,
    )
    return _serialize_agent(agent)


@router.get("", response_model=list[AgentResponse])
def list_agents(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # admin + owner 都應該看到所有 agent。先前單獨檢查 "admin" 會把
    # owner 推到 else 的 owner_user_id 過濾,讓 owner 看不到不是自己
    # 註冊的 agent — 造成「CSP UI 顯示 2 個、ANILA UI 顯示 3 個」的
    # 錯覺,以為刪除沒同步,實際上 agent 還在 DB,只是被 UI 藏起來。
    if is_admin_tier(current_user):
        agents = db.query(Agent).order_by(Agent.created_at.desc()).all()
    else:
        agents = db.query(Agent).filter(Agent.owner_user_id == current_user.id).all()
    return [_serialize_agent(a) for a in agents]


@router.get("/{agent_id}", response_model=AgentResponse)
def get_agent(
    agent_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    ensure_agent_view_access(agent, current_user)
    return _serialize_agent(agent)


@router.put("/{agent_id}", response_model=AgentResponse)
def update_agent(
    agent_id: int,
    payload: AgentUpdateRequest,
    http_request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Owner / admin may edit description, endpoint, capabilities, etc.

    Delete stays admin-only (``DELETE /api/agents/{id}``), so developers
    get self-service maintenance of their own agent's metadata without
    ever being able to remove it — which would orphan historical
    conversations and audit entries that reference the agent by id.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    _require_agent_editor(agent, current_user)

    patch = payload.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(status_code=400, detail="沒有提供要更新的欄位")

    # P4.7 — collection binding fields are not Agent columns; resolve the
    # set and drop them from the attribute patch before setattr.
    fields_set = payload.model_fields_set
    requested_ids = resolve_requested_collection_ids(
        collection_ids=payload.collection_ids,
        collection_id=payload.collection_id,
        collection_ids_provided="collection_ids" in fields_set,
        collection_id_provided="collection_id" in fields_set,
    )
    patch.pop("collection_ids", None)
    patch.pop("collection_id", None)

    # doc 05 §4 — a submitted manifest is validated fail-closed (422) and
    # mapped onto ``manifest_json`` (the ``manifest`` request field is not a
    # column). Explicit ``null`` clears the stored snapshot.
    if "manifest" in patch:
        raw_manifest = patch.pop("manifest")
        patch["manifest_json"] = (
            validate_agent_manifest(raw_manifest) if raw_manifest is not None else None
        )

    # Classification level is applied via the shared helper so the legacy
    # boolean stays derived from the same mirror threshold as conversations.
    # Developers may not lower the effective policy level (incl. legacy floor).
    # Audit is driven by the *effective* transition (not stored-column alone).
    level_change: (
        tuple[
            ClassificationLevel,
            ClassificationLevel,
            ClassificationLevel,
            ClassificationLevel,
        ]
        | None
    ) = None
    level_fields_touched = False
    if "default_classification_level" in patch:
        raw_level = patch.pop("default_classification_level")
        if raw_level is None:
            raise HTTPException(
                status_code=400, detail="預設分類等級不可設為空值"
            )
        new_level = (
            raw_level
            if isinstance(raw_level, ClassificationLevel)
            else parse_stored_classification_level(raw_level)
        )
        refuse_classification_downgrade(agent, new_level, current_user)
        effective_transition, changed, stored_transition = (
            apply_default_classification_level(
                agent,
                new_level,
                db=db,
                allow_downgrade=is_admin_tier(current_user),
            )
        )
        if changed:
            level_fields_touched = True
            if effective_transition is not None:
                from_eff, to_eff = effective_transition
                from_stored, to_stored = stored_transition
                level_change = (from_eff, to_eff, from_stored, to_stored)

    # SSRF guard — endpoint_url 變更時重新驗證；同時把 approval_status 退回
    # pending，避免 owner 把已核可 agent 的 endpoint 改到內網（H4）。
    endpoint_changed = (
        "endpoint_url" in patch
        and patch["endpoint_url"] is not None
        and patch["endpoint_url"] != agent.endpoint_url
    )
    if endpoint_changed:
        _enforce_endpoint_url(patch["endpoint_url"])

    # 底層模型就是這個 agent 實際呼叫的模型。換成別顆，或在下線後
    # 重新指到一顆仍啟用的模型，都要退回 registered 再送審。
    base_model_reapproval = False
    if "base_model_id" in patch:
        new_id = patch["base_model_id"]
        if new_id is None:
            raise HTTPException(status_code=400, detail="底層模型不可設為空值")
        from app.models.model_registry import ModelRegistry
        base = db.query(ModelRegistry).filter(ModelRegistry.id == new_id).first()
        if base is None:
            raise HTTPException(status_code=400, detail=f"底層模型 id={new_id} 不存在")
        if not base.is_active:
            raise HTTPException(
                status_code=400,
                detail=f"底層模型「{base.display_name}」已停用",
            )
        model_changed = new_id != agent.base_model_id
        resubmit_after_offline = bool(agent.unavailable_reason) and not model_changed
        if model_changed or resubmit_after_offline:
            base_model_reapproval = True

    from_collection_ids: list[int] | None = None
    to_collection_ids: list[int] | None = None
    if requested_ids is not None:
        from_collection_ids = get_bound_collection_ids(agent)
        # Validate the DELTA (newly added ids only), not the whole submitted
        # set. Console always resends every bound id; re-gating pre-existing
        # rows 403s the owner as soon as one binding goes stale (e.g.
        # collection transferred away) — including when they only add a
        # different collection or edit an unrelated field.
        # Removals need only the agent-editor right (already checked above).
        # Safe: search independently filters via ``_require_collection_access``
        # on the owner principal, so a grandfathered stale binding grants
        # nothing at read time.
        added_ids = sorted(set(requested_ids) - set(from_collection_ids))
        if added_ids:
            owner = (
                db.query(User).filter(User.id == agent.owner_user_id).first()
            )
            if owner is None:
                raise HTTPException(
                    status_code=500, detail="agent owner 不存在"
                )
            _validate_collection_access_for_ids(db, owner, added_ids)
            # P4.9 — only the DELTA is gated (same shape as access check).
            # Grandfathered over-level bindings must not brick unrelated edits.
            refuse_bind_above_agent_level(db, agent, added_ids)
        if requested_ids != from_collection_ids:
            to_collection_ids = set_bound_collection_ids(db, agent, requested_ids)

    changed_level_fields = (
        ["default_classification_level", "requires_encryption"]
        if level_fields_touched
        else []
    )
    changed: list[str] = list(changed_level_fields)
    for field, value in patch.items():
        if getattr(agent, field) != value:
            setattr(agent, field, value)
            changed.append(field)
    if to_collection_ids is not None:
        changed.append("bound_collection_ids")
    if base_model_reapproval and agent.unavailable_reason:
        agent.unavailable_reason = None
        changed.append("unavailable_reason")
    # 端點或底層模型一變，已核准的組合就失效，退回 registered 再送審。
    reapproval_required = (
        agent.approval_status == ApprovalStatus.APPROVED.value
        and (endpoint_changed or base_model_reapproval)
    )
    if not changed and not reapproval_required:
        return _serialize_agent(agent)

    if reapproval_required:
        agent.approval_status = REGISTER_DEFAULT_APPROVAL
        agent.approved_by = None
        agent.approved_at = None
        changed.append("approval_status->registered")

    # Governance convention: audit in the same transaction, check result,
    # abort with 500 before commit if the audit row could not be written.
    # Classification audit when the *effective* policy level changed
    # (legacy-floor drop included); boolean-only same-effective repair
    # commits without a set_classification row.
    if level_change is not None:
        from_eff, to_eff, from_stored, to_stored = level_change
        level_audit = log_audit_event(
            db,
            actor=current_user,
            action="set_classification",
            resource_type="agent",
            resource_id=agent.id,
            detail=(
                f"變更 agent「{agent.name}」有效分類等級："
                f"{from_eff.to_storage()} → {to_eff.to_storage()}"
            ),
            ip_address=_client_ip(http_request),
            metadata={
                "from_level": from_eff.to_storage(),
                "to_level": to_eff.to_storage(),
                "from_stored_level": from_stored.to_storage(),
                "to_stored_level": to_stored.to_storage(),
                "requires_controlled_access": agent.requires_encryption,
            },
            commit=False,
        )
        if level_audit is None:
            raise HTTPException(
                status_code=500,
                detail="稽核紀錄寫入失敗，分類等級變更未生效",
            )
    if to_collection_ids is not None and from_collection_ids is not None:
        bind_audit = log_audit_event(
            db,
            actor=current_user,
            action="update_bound_collections",
            resource_type="agent",
            resource_id=agent.id,
            detail=(
                f"變更 agent「{agent.name}」綁定知識庫："
                f"{from_collection_ids} → {to_collection_ids}"
            ),
            ip_address=_client_ip(http_request),
            metadata={
                "from_collection_ids": from_collection_ids,
                "to_collection_ids": to_collection_ids,
            },
            commit=False,
        )
        if bind_audit is None:
            raise HTTPException(
                status_code=500,
                detail="稽核紀錄寫入失敗，知識庫綁定變更未生效",
            )
    other_changed = [
        f
        for f in changed
        if f not in changed_level_fields and f != "bound_collection_ids"
    ]
    if other_changed:
        update_audit = log_audit_event(
            db,
            actor=current_user,
            action="update",
            resource_type="agent",
            resource_id=agent.id,
            detail=f"更新 agent「{agent.name}」：" + ", ".join(changed),
            ip_address=_client_ip(http_request),
            commit=False,
        )
        if update_audit is None:
            raise HTTPException(
                status_code=500,
                detail="稽核紀錄寫入失敗，agent 更新未生效",
            )

    db.commit()
    db.refresh(agent)
    return _serialize_agent(agent)


@router.delete("/{agent_id}")
def delete_agent(
    agent_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    agent_name = agent.name
    db.delete(agent)
    db.commit()
    log_audit_event(
        db, actor=admin, action="delete", resource_type="agent",
        resource_id=agent_id, detail=f"刪除 agent「{agent_name}」",
        ip_address=_client_ip(request), commit=True,
    )
    return {"message": f"已刪除 agent「{agent_name}」"}
