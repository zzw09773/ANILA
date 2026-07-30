"""Agent endpoint probes (manual health-check + csk- test-connection).

Split verbatim from the former single-module ``app/api/agents.py``
(behavior-preserving refactor).
"""
import asyncio
import time
import uuid
from datetime import datetime, timezone

import httpx
from anila_core.security import UnsafeEndpointError, validate_outbound_url
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.agent import Agent
from app.models.trace_span import TraceSpan
from app.models.user import User
from app.schemas.contracts.agents import (
    TRACE_TEST_ELIGIBLE_STATES,
    TraceTestItem,
    TraceTestItemStatus,
    TraceTestReport,
)
from app.services import agent_credential_service
from app.services.audit_service import log_audit_event
from app.services.auth_service import is_admin_tier, require_admin
from app.services.proxy.urls import join_upstream_path

from app.api.agents._common import (
    _client_ip,
    _require_developer_or_admin,
    _resolve_agent,
)

router = APIRouter()

# Bounded poll window for agent-emitted spans to arrive in ``trace_spans``
# via the Full Trace callback (POST /v1/traces/{trace_id}/spans). Kept small
# and module-level so tests can monkeypatch a fast timeout.
_TRACE_TEST_POLL_TIMEOUT_S = 10.0
_TRACE_TEST_POLL_INTERVAL_S = 0.25

# doc 06 §6 — the mandatory (non-conditional) span types a real run must emit
# to leave dev/test: run / model_call / output start+finish pairs. retrieval /
# tool_call / error / step are conditional or triggerable and reported as
# informational, not pass-blocking.
_TRACE_TEST_REQUIRED_SPAN_TYPES: tuple[str, ...] = (
    "agent.run.started",
    "agent.run.finished",
    "agent.model_call.started",
    "agent.model_call.finished",
    "agent.output.started",
    "agent.output.finished",
)


@router.post("/{agent_id}/health-check")
async def trigger_agent_health_check(
    agent_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Probe an agent's endpoint and update ``health_status``.

    Mirrors ``POST /api/models/{id}/health-check`` for parity on the
    management UI: the admin clicks "檢查", the backend tries a few
    common liveness paths, and the DB stamp is updated so the colored
    dot in the agents list reflects reality.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")

    ip = _client_ip(request)
    # Call-time SSRF guard — refuse to probe an endpoint that fails outbound
    # validation (TOCTOU / DNS-rebinding defense), even for an admin ping.
    # Guard once per host (scheme/hostname only; getaddrinfo is blocking),
    # then build the three probe URLs.
    try:
        validate_outbound_url(agent.endpoint_url, endpoint_kind="agent")
    except UnsafeEndpointError as exc:
        agent.health_status = "unhealthy"
        db.commit()
        log_audit_event(
            db, actor=admin, action="health_check",
            resource_type="agent", resource_id=agent.id,
            status="failure",
            detail=f"健康檢查拒絕: 端點未通過出向安全驗證 ({exc})",
            ip_address=ip,
            commit=True,
        )
        return {"status": "unhealthy", "detail": f"端點未通過出向安全驗證: {exc}"}
    probe_paths = ["/health", "/v1/models", "/"]
    probe_urls = [join_upstream_path(agent.endpoint_url, path) for path in probe_paths]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for path, url in zip(probe_paths, probe_urls):
                try:
                    resp = await client.get(url)
                    if resp.status_code < 500:
                        agent.health_status = "healthy"
                        db.commit()
                        log_audit_event(
                            db, actor=admin, action="health_check",
                            resource_type="agent", resource_id=agent.id,
                            detail=f"手動健康檢查成功: {agent.name}",
                            ip_address=ip,
                            commit=True,
                        )
                        return {
                            "status": "healthy",
                            "detail": f"端點 {path} 回應 {resp.status_code}",
                        }
                except httpx.ConnectError:
                    continue
            agent.health_status = "unhealthy"
            db.commit()
            log_audit_event(
                db, actor=admin, action="health_check",
                resource_type="agent", resource_id=agent.id,
                detail=f"手動健康檢查離線: {agent.name}",
                ip_address=ip,
                commit=True,
            )
            return {"status": "unhealthy", "detail": "無法連線到 agent 端點"}
    except Exception as e:
        agent.health_status = "unhealthy"
        db.commit()
        log_audit_event(
            db, actor=admin, action="health_check",
            resource_type="agent", resource_id=agent.id,
            status="failure",
            detail=f"手動健康檢查失敗: {agent.name} ({e})",
            ip_address=ip,
            commit=True,
        )
        return {"status": "unhealthy", "detail": str(e)}


class TestConnectionResponse(BaseModel):
    reachable: bool
    # None = could not determine (endpoint unreachable).
    token_accepted: bool | None = None
    status_code: int | None = None
    detail: str


@router.post("/{agent_id}/test-connection", response_model=TestConnectionResponse)
async def test_agent_connection(
    agent_id: int,
    request: Request,
    current_user: User = Depends(_require_developer_or_admin),
    db: Session = Depends(get_db),
):
    """Probe the agent endpoint with its OWN csk- to confirm the operator wired
    ``CSP_SERVICE_TOKEN`` into the agent's .env (S-Q3). Owner-or-admin.

    Sends an empty ``messages`` body so the agent's inbound token check fires
    *before* any LLM work: 401 → the agent rejected our csk- (missing/wrong in
    .env); anything else (e.g. 400 "no user message") → token accepted, .env
    correctly wired. Connection error / timeout → unreachable.
    """
    agent = _resolve_agent(db, agent_id)
    if not is_admin_tier(current_user) and agent.owner_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="無權限測試此 Agent")

    # Call-time SSRF guard (TOCTOU / DNS-rebinding), same as health-check.
    # Guard the FINAL url that will actually be requested.
    url = join_upstream_path(agent.endpoint_url, "/v1/chat/completions")
    try:
        validate_outbound_url(url, endpoint_kind="agent")
    except UnsafeEndpointError as exc:
        raise HTTPException(status_code=400, detail=f"端點未通過出向安全驗證: {exc}")

    # The token the Router would present == whatever
    # get_active_plaintext_for_agent selects for outbound dispatch. Reuse it
    # so the probe tests the SAME credential CSP actually sends (consistent
    # ordering, incl. after a rotate of a non-latest credential — Nit#2).
    token = agent_credential_service.get_active_plaintext_for_agent(
        db, agent_id=agent.id
    )
    if not token:
        raise HTTPException(
            status_code=409,
            detail="此 Agent 尚無有效憑證,請先核發 csk- 再測試連線",
        )

    ip = _client_ip(request)
    body = {"model": agent.name, "messages": [], "stream": False}
    headers = {"X-CSP-Service-Token": token}
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(url, json=body, headers=headers)
        accepted = resp.status_code != 401
        detail = (
            "端點接受了該 csk-(agent .env 的 CSP_SERVICE_TOKEN 配對正確)"
            if accepted
            else "端點以 401 拒絕該 csk-(agent .env 未設或不符)"
        )
        log_audit_event(
            db, actor=current_user, action="test_connection",
            resource_type="agent", resource_id=agent.id,
            status="success" if accepted else "failure",
            detail=f"測試連線 → HTTP {resp.status_code}", ip_address=ip, commit=True,
        )
        return TestConnectionResponse(
            reachable=True, token_accepted=accepted,
            status_code=resp.status_code, detail=detail,
        )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as exc:
        log_audit_event(
            db, actor=current_user, action="test_connection",
            resource_type="agent", resource_id=agent.id, status="failure",
            detail=f"測試連線無法連線: {exc}", ip_address=ip, commit=True,
        )
        return TestConnectionResponse(
            reachable=False, token_accepted=None,
            detail=f"無法連線到 agent 端點: {exc}",
        )


async def _poll_trace_spans(
    db: Session, trace_id: str, *, timeout_s: float, interval_s: float
) -> list[TraceSpan]:
    """Bounded poll for agent-emitted spans landing under ``trace_id``.

    Each round runs a SELECT, then ``db.rollback()`` before either returning
    or ``await``-ing sleep so the SELECT's transaction is never held across
    the wait. Returns as soon as any span is seen, or an empty list once the
    deadline lapses. Under READ COMMITTED each statement sees newly committed
    rows, so releasing between rounds does not hide late-arriving spans.
    """
    deadline = time.monotonic() + max(timeout_s, 0.0)
    while True:
        rows = (
            db.query(TraceSpan).filter(TraceSpan.trace_id == trace_id).all()
        )
        if rows or time.monotonic() >= deadline:
            # End the SELECT's transaction before returning to the caller.
            db.rollback()
            return rows
        # Release before sleep — holding the SELECT txn across await leaves
        # idle-in-transaction for up to _TRACE_TEST_POLL_TIMEOUT_S.
        db.rollback()
        await asyncio.sleep(interval_s)


def _evaluate_trace_test(
    spans: list[TraceSpan],
    *,
    reachable: bool,
    token_accepted: bool | None,
    classification_level: str,
    has_manifest: bool,
) -> list[TraceTestItem]:
    """Build the doc 06 §8 checklist from a synthetic run's observed spans.

    Required (pass-blocking): endpoint reachable, service token accepted, spans
    received, required span types complete, parentage reaches a single root.
    Conditional / not-yet-checkable items (manifest / SSE / classification echo
    / error path / citations) are reported as SKIPPED and do not block.
    """
    P, F, S = (
        TraceTestItemStatus.PASSED,
        TraceTestItemStatus.FAILED,
        TraceTestItemStatus.SKIPPED,
    )
    items: list[TraceTestItem] = []

    def add(name: str, status: TraceTestItemStatus, required: bool, detail: str = "") -> None:
        items.append(
            TraceTestItem(name=name, status=status, required=required, detail=detail)
        )

    # 1. endpoint reachable / 3. /v1/chat/completions reachable (合併:一次派發即測)
    add(
        "endpoint_reachable",
        P if reachable else F,
        True,
        "端點回應 /v1/chat/completions" if reachable else "端點無法連線",
    )
    # 2. service token valid
    if not reachable:
        add("service_token_accepted", S, True, "端點無法連線,無法判定 token")
    else:
        add(
            "service_token_accepted",
            P if token_accepted else F,
            True,
            "端點接受該 csk-" if token_accepted else "端點以 401 拒絕該 csk-",
        )

    span_types = [s.span_type for s in spans]
    type_set = set(span_types)

    # 6. anila.spans received
    add(
        "spans_received",
        P if spans else F,
        True,
        f"收到 {len(spans)} 個 span" if spans else "逾時未收到任何 span",
    )
    # 7. required span types complete (core run/model_call/output pairs)
    missing = [t for t in _TRACE_TEST_REQUIRED_SPAN_TYPES if t not in type_set]
    add(
        "required_span_types",
        P if (spans and not missing) else F,
        True,
        "必備 span type 齊備" if (spans and not missing) else f"缺少:{missing}",
    )
    # parentage reaches a single root
    span_ids = {s.span_id for s in spans}
    roots = [
        s for s in spans if s.parent_span_id is None or s.parent_span_id not in span_ids
    ]
    add(
        "parentage_single_root",
        P if len(roots) == 1 else F,
        True,
        "span 樹收斂至單一根" if len(roots) == 1 else f"根 span 數={len(roots)}",
    )

    # ── 非 pass-blocking:條件式 / 目前未可查(SKIPPED)────────────────────────
    from app.schemas.contracts.traces import REQUIRED_AGENT_SPAN_TYPES

    full_missing = [t for t in REQUIRED_AGENT_SPAN_TYPES if t not in type_set]
    add(
        "full_trace_13_complete",
        P if (spans and not full_missing) else S,
        False,
        "13 型別全齊" if not full_missing else f"尚缺 {len(full_missing)} 型別(非必要)",
    )
    # 4. manifest valid — 註冊時已 fail-closed 驗過;此處僅回報是否有 manifest。
    add(
        "manifest_valid",
        P if has_manifest else S,
        False,
        "已存 manifest_json(註冊時驗過)" if has_manifest else "無 manifest,略過",
    )
    # 5. SSE valid — 採 callback(POST /v1/traces)回報,未測 SSE 通道。
    add("sse_channel", S, False, "採 callback 回報,未測 SSE 通道")
    # 8. classification header respected — 檢查 agent 是否於 span attributes 回報等級。
    echoed = any(
        (s.attributes or {}).get("classification_level") == classification_level
        for s in spans
    )
    add(
        "classification_respected",
        P if echoed else S,
        False,
        f"span 回報分類等級={classification_level}"
        if echoed
        else "agent 未於 span attributes 回報分類等級,略過",
    )
    # error handling(optional item)— 合成測試不觸發錯誤路徑。
    add("error_handling", S, False, "未於合成測試觸發錯誤路徑,略過")
    # citations(conditional)— 有 retrieval span 才適用。
    retrieval_spans = [s for s in spans if s.span_type.startswith("agent.retrieval")]
    if not retrieval_spans:
        add("citations_attributes", S, False, "未使用檢索,略過")
    else:
        cited = any(
            (s.attributes or {}).get("collection_ids") or (s.attributes or {}).get("document_ids")
            for s in retrieval_spans
        )
        add(
            "citations_attributes",
            P if cited else F,
            False,
            "retrieval span 帶引用屬性" if cited else "retrieval span 缺 collection/document 引用",
        )
    return items


@router.post("/{agent_id}/trace-test", response_model=TraceTestReport)
async def run_agent_trace_test(
    agent_id: int,
    request: Request,
    current_user: User = Depends(_require_developer_or_admin),
    db: Session = Depends(get_db),
) -> TraceTestReport:
    """On-demand Full Trace 診斷(不再是核准硬閘)。

    以合成 trace_id 對 ``{endpoint}/v1/chat/completions`` 發一次最小 chat run,
    帶 ``X-ANILA-Trace-Id`` / ``X-ANILA-Task-Id`` / ``X-ANILA-Classification-Level``
    (與 test-connection 同一套 csk- + SSRF guard),再有界輪詢 ``trace_spans``,
    逐項評估檢核表並寫入 ``trace_test_report``。OE-1:**不**推進
    ``approval_status``、通過與否都不阻擋 admin 核准。
    """
    agent = _resolve_agent(db, agent_id)
    if not is_admin_tier(current_user) and agent.owner_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="無權限測試此 Agent")

    if agent.approval_status not in TRACE_TEST_ELIGIBLE_STATES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Agent 目前狀態「{agent.approval_status}」不可執行 trace-test"
                "(僅 registered / approved 適用)"
            ),
        )

    # Call-time SSRF guard (TOCTOU / DNS-rebinding), same as test-connection.
    # Guard the FINAL url that will actually be requested.
    url = join_upstream_path(agent.endpoint_url, "/v1/chat/completions")
    try:
        validate_outbound_url(url, endpoint_kind="agent")
    except UnsafeEndpointError as exc:
        raise HTTPException(status_code=400, detail=f"端點未通過出向安全驗證: {exc}")

    token = agent_credential_service.get_active_plaintext_for_agent(
        db, agent_id=agent.id
    )
    if not token:
        raise HTTPException(
            status_code=409,
            detail="此 Agent 尚無有效憑證,請先核發 csk- 再執行 trace-test",
        )

    ip = _client_ip(request)
    trace_id = f"tracetest-{uuid.uuid4().hex}"
    synthetic_task_id = f"tracetest-task-{uuid.uuid4().hex}"
    classification = agent.default_classification_level or "無機密"

    body = {
        "model": agent.name,
        "messages": [{"role": "user", "content": "ANILA trace-test ping"}],
        "stream": False,
        "metadata": {"task_id": synthetic_task_id, "trace_id": trace_id},
    }
    headers = {
        "X-CSP-Service-Token": token,
        "X-ANILA-Trace-Id": trace_id,
        "X-ANILA-Task-Id": synthetic_task_id,
        "X-ANILA-Classification-Level": classification,
    }

    reachable = False
    token_accepted: bool | None = None
    # Release any read txn so the agent's callback session can commit spans on
    # the shared connection before we poll (see _poll_trace_spans).
    db.rollback()
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(url, json=body, headers=headers)
        reachable = True
        token_accepted = resp.status_code != 401
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError):
        reachable = False

    spans: list[TraceSpan] = []
    if reachable and token_accepted:
        spans = await _poll_trace_spans(
            db,
            trace_id,
            timeout_s=_TRACE_TEST_POLL_TIMEOUT_S,
            interval_s=_TRACE_TEST_POLL_INTERVAL_S,
        )

    items = _evaluate_trace_test(
        spans,
        reachable=reachable,
        token_accepted=token_accepted,
        classification_level=classification,
        has_manifest=bool(getattr(agent, "manifest_json", None)),
    )
    passed = all(
        item.status == TraceTestItemStatus.PASSED for item in items if item.required
    )
    report = TraceTestReport(
        passed=passed,
        trace_id=trace_id,
        items=items,
        checked_at=datetime.now(timezone.utc),
    )

    # Persist the diagnostic report either way. OE-1: do NOT advance
    # approval_status — trace-test is on-demand only. Stamp
    # trace_test_passed_at on pass so operators can still see history.
    agent.trace_test_report = report.model_dump(mode="json")
    if passed:
        agent.trace_test_passed_at = datetime.now(timezone.utc)
    db.commit()

    log_audit_event(
        db, actor=current_user, action="trace_test", resource_type="agent",
        resource_id=agent.id, status="success" if passed else "failure",
        detail=(
            f"trace-test {'通過' if passed else '未通過'}(trace_id={trace_id})"
        ),
        ip_address=ip, commit=True,
    )
    return report
