"""P1-8 RunState — HITL pause / resume + tool approval。

本模組對應 enhancement roadmap §4.2 **P1-8**:把 agent run 抽成一個
可序列化的 ``RunState`` 快照,作為 human-in-the-loop(HITL)的暫停 /
恢復邊界。對齊上游 ``openai-agents-python/src/agents/run_state.py`` 的
``RunState`` 設計,但 ANILA 階段只 port「pause / approve / deny / resume」
核心子集,序列化用 deterministic JSON 與 P1-1 :mod:`prompt_cache`
``_canonical_dumps`` 同一套規則,避免兩處 drift。

# 為什麼需要 RunState

agent 跑 multi-turn 過程中,某些操作需要 human approve,例如:

* file write / shell exec(對齊 P0-7 PolicyEngine 的 ASK effect)
* paid API call(對齊 P1-15 CostTracker 的 budget 警告)
* guardrail 抓到敏感資料(對齊 P0-6 GuardrailTripwireTriggered)
* token budget 超標(對齊 P1-9 BudgetExceeded)

這些情況下 runner 不應直接拒絕或繼續,而是「凍結整個 run、回給 caller
一個 ``RunState`` 快照」,等人在 CLI / Web UI 上 review 完再 resume。
``RunState`` 必須:

1. **完整 capture run 進度** — messages history、in-flight tool calls、
   approved / denied 清單、metadata。
2. **deterministic 序列化** — 同 state → 同 bytes,方便 hash / diff /
   diff-test。dict key 一律 sort,UTF-8 不 escape ASCII,datetime 用
   ISO-8601 with timezone。
3. **dataclass-only**,不含 callable / weakref / open file。

# 與其他 P 票的關係

* **P0-1 hooks**:`HITLController.pause` / `resume` 可在進入 / 離開
  pause 狀態時 fire ``GUARDRAIL_TRIPWIRE`` / ``STOP`` / ``AGENT_START``
  等對應事件(本 task 不強制接,留給 P1-11 真實 SDK 整合時補上)。
* **P0-9 tracing**:每次 pause / resume 都開一個 ``hitl.pause`` span
  並用 ``metadata`` 紀錄 :class:`PauseReason`、pending tool ids 等。
* **P1-3 hook_context**:``RunState.metadata`` 與 ``OperationContext.metadata``
  共用 dict 介面,callers 可把 session / turn / operation id 串起來。
* **P1-7 streaming**:``AnilaStreamRunner`` 不直接綁 RunState;整合方式為
  「runner 看到 paused state 就停 yield、return」,由 caller 決定下一步。
  本模組提供 :func:`pause_streaming_if_needed` helper 給 streaming 用。

# 不做的事

* 不 port 上游 schema versioning 機制(`CURRENT_SCHEMA_VERSION`)。ANILA
  P1-8 還在 in-tree,序列化格式之後可隨時 renumber。
* 不 port `RunContextWrapper` / `ModelResponse` / `RunItem` 等 SDK 內部型別。
  ``messages`` 改用 P1-1 已定義的 ``Message = Mapping[str, Any]``。
* 不做 file lock。``JsonFileStateStore`` 預設單 process / 單 thread 使用;
  若日後需要跨 process,改 wrap atomic rename + flock 即可。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from anila_agent.core.concurrency import ToolCall

# ---------------------------------------------------------------------------
# 型別 / enum
# ---------------------------------------------------------------------------


# RunState lifecycle 狀態。對齊上游 ``RunState`` 的 ``status`` 但收斂為 5 種:
# - ``"running"``:正在跑,尚未進入暫停。
# - ``"paused_for_approval"``:有 pending tool call 等 human approve。
# - ``"paused_for_input"``:agent 需要額外 user input 才能繼續。
# - ``"completed"``:run 完成,``final_output`` 已填。
# - ``"failed"``:run 因 exception / guardrail tripwire 失敗。
RunStatus = Literal[
    "running",
    "paused_for_approval",
    "paused_for_input",
    "completed",
    "failed",
]


class PauseReason(str, Enum):
    """RunState 進入 paused 狀態的可能原因。

    四種對應 ANILA 不同子系統觸發 pause:

    * ``TOOL_APPROVAL``:tool call 需要 human approve(對齊 P0-7 ASK effect)。
    * ``USER_INPUT_REQUIRED``:agent 需要 user 補充 context / 回答問題。
    * ``GUARDRAIL_TRIPWIRE``:P0-6 guardrail 觸發 tripwire,要求人工裁決。
    * ``BUDGET_EXCEEDED``:P1-9 / P1-15 token / cost budget 警告,要求人工確認。

    繼承 ``str`` 讓 enum 直接可 JSON-serialize(同上游 :class:`HookEvent`)。
    """

    TOOL_APPROVAL = "tool_approval"
    USER_INPUT_REQUIRED = "user_input_required"
    GUARDRAIL_TRIPWIRE = "guardrail_tripwire"
    BUDGET_EXCEEDED = "budget_exceeded"


# Message 與 P1-1 ``prompt_cache.Message`` 完全相容(``Mapping[str, Any]``);
# 此處 re-alias 避免互相 import 形成循環。
Message = Mapping[str, Any]


# ---------------------------------------------------------------------------
# Deterministic JSON serialization — 與 prompt_cache._canonical_dumps 同規則
# ---------------------------------------------------------------------------


def _canonical_dumps(obj: Any) -> str:
    """Deterministic JSON 序列化。

    與 :func:`anila_agent.core.prompt_cache._canonical_dumps` 同設定:
    ``sort_keys=True`` / ``separators=(",", ":")`` / ``ensure_ascii=False``。
    本檔案複製一份避免從 prompt_cache import(prompt_cache 也 import 本檔的
    型別 alias 會造成 circular)。
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _iso_now() -> datetime:
    """timezone-aware UTC 當下時間。

    所有 RunState 內的 datetime 一律 UTC + tz-aware,序列化 / 反序列化
    才能完全 round-trip。
    """
    return datetime.now(timezone.utc)


def _serialize_datetime(value: datetime) -> str:
    """datetime → ISO-8601 字串(保留 tz)。"""
    return value.isoformat()


def _deserialize_datetime(value: str) -> datetime:
    """ISO-8601 字串 → tz-aware datetime。

    Python 3.11+ ``datetime.fromisoformat`` 已可解 timezone suffix(``"+00:00"``);
    為 3.10 相容,額外處理 ``"Z"`` 後綴(JSON UI 常用)。
    """
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _serialize_tool_call(tc: ToolCall) -> dict[str, Any]:
    """ToolCall → JSON-friendly dict(deterministic)。

    ``ToolCall`` 是 ``frozen=True`` dataclass,直接 dataclasses.asdict 即可,
    但 ``args`` 內可能含巢狀 dict;我們依賴 ``_canonical_dumps`` 在最外層
    sort,所以這裡只需要原樣回傳即可。
    """
    return {
        "name": tc.name,
        "args": dict(tc.args),
        "call_id": tc.call_id,
    }


def _deserialize_tool_call(data: Mapping[str, Any]) -> ToolCall:
    """dict → ToolCall(防壞資料)。"""
    return ToolCall(
        name=str(data.get("name", "")),
        args=dict(data.get("args", {}) or {}),
        call_id=str(data.get("call_id", "")),
    )


# ---------------------------------------------------------------------------
# RunState dataclass
# ---------------------------------------------------------------------------


@dataclass
class RunState:
    """單一 agent run 的可序列化快照。

    ``RunState`` 是 HITL pause / resume 的 durable 邊界;設計上 **所有欄位
    都是 JSON-friendly**,沒有 callable、open file、weakref。

    與上游 SDK ``RunState`` 的差異:

    * 不含 ``RunContextWrapper`` / ``ModelResponse`` 等 SDK-內部型別;
      conversation history 改用 P1-1 通用 ``Message = Mapping[str, Any]``。
    * ``messages`` 是 ``list[Message]``(可變),方便 caller append 新訊息;
      ``to_json`` 序列化時會逐筆轉 dict,所以原 list 內含 ``dict[str, Any]``
      也 OK。
    * ``pause_reason`` 與 ``paused_at`` 對齊 :class:`PauseReason` enum。

    Attributes:
        run_id: 此 run 的唯一識別字。預設為 uuid4 hex,方便檔名 / log 連動。
        agent_name: 跑此 run 的 agent 名稱(對齊 ``AgentStartInput.agent_name``)。
        status: 目前生命週期狀態(見 :data:`RunStatus`)。
        messages: 對話訊息(順序即 timeline)。Message 內 schema 不限制,但
            序列化會用 ``sort_keys`` 確保 deterministic。
        pending_tool_calls: 等 human approve 的 :class:`ToolCall` 清單。
        approved_tool_call_ids: 已 approve 的 ``call_id`` 清單(保留審計用)。
        denied_tool_call_ids: 已 deny 的 ``call_id`` 清單。
        started_at: run 起始時間(tz-aware UTC)。
        paused_at: 進入 paused 狀態的時間;running / completed / failed 時為 ``None``。
        pause_reason: 進入 paused 狀態的原因(對應 :class:`PauseReason`);
            running / completed / failed 時為 ``None``。
        deny_reasons: ``{call_id: reason}`` 紀錄每筆 deny 的理由(audit 用)。
        final_output: ``status="completed"`` 時的 agent 最終輸出。
        failure_reason: ``status="failed"`` 時的失敗原因 string。
        metadata: 任意 caller-defined metadata。要保證 JSON-friendly,否則
            ``to_json`` 會 raise。
    """

    run_id: str
    agent_name: str
    status: RunStatus = "running"
    messages: list[dict[str, Any]] = field(default_factory=list)
    pending_tool_calls: list[ToolCall] = field(default_factory=list)
    approved_tool_call_ids: list[str] = field(default_factory=list)
    denied_tool_call_ids: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=_iso_now)
    paused_at: datetime | None = None
    pause_reason: PauseReason | None = None
    deny_reasons: dict[str, str] = field(default_factory=dict)
    final_output: Any = None
    failure_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # factory / serialization
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        agent_name: str,
        *,
        run_id: str | None = None,
        messages: Iterable[Message] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RunState:
        """建立一個全新 running 狀態的 ``RunState``。

        Args:
            agent_name: 跑此 run 的 agent 名稱。
            run_id: 自訂 id;不給就 uuid4 hex。
            messages: 初始 conversation history(會被 deep-copied 為 list[dict])。
            metadata: 初始 metadata(會 shallow copy 進 ``self.metadata``)。

        Returns:
            一個 status="running" 的新 instance。
        """
        return cls(
            run_id=run_id or uuid.uuid4().hex,
            agent_name=agent_name,
            status="running",
            messages=[dict(m) for m in (messages or [])],
            metadata=dict(metadata or {}),
        )

    def to_json(self) -> str:
        """把整個 state 序列化為 deterministic JSON 字串。

        關鍵性質:
        * 同 state 內容 → 同 bytes(``sort_keys=True``)。
        * datetime 用 ISO-8601 with timezone。
        * Enum 用 ``.value`` 字串。
        * ``ToolCall`` 用 :func:`_serialize_tool_call` 拆成 dict。

        Returns:
            JSON 字串(UTF-8 字元;不 escape ASCII)。

        Raises:
            TypeError: 若 ``metadata`` 內含非 JSON-friendly 值(如 set / object)。
        """
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "agent_name": self.agent_name,
            "status": self.status,
            "messages": [dict(m) for m in self.messages],
            "pending_tool_calls": [
                _serialize_tool_call(tc) for tc in self.pending_tool_calls
            ],
            "approved_tool_call_ids": list(self.approved_tool_call_ids),
            "denied_tool_call_ids": list(self.denied_tool_call_ids),
            "started_at": _serialize_datetime(self.started_at),
            "paused_at": (
                _serialize_datetime(self.paused_at)
                if self.paused_at is not None
                else None
            ),
            "pause_reason": (
                self.pause_reason.value if self.pause_reason is not None else None
            ),
            "deny_reasons": dict(self.deny_reasons),
            "final_output": self.final_output,
            "failure_reason": self.failure_reason,
            "metadata": dict(self.metadata),
        }
        return _canonical_dumps(payload)

    @classmethod
    def from_json(cls, s: str) -> RunState:
        """從 :meth:`to_json` 產出的字串反序列化回 ``RunState``。

        Args:
            s: ``to_json`` 產出的 JSON 字串。

        Returns:
            還原後的 ``RunState`` instance(與原 instance 內容相等,但是新物件)。

        Raises:
            ValueError: 若 JSON 結構不符 schema(缺欄位 / status 不合法等)。
        """
        data = json.loads(s)
        if not isinstance(data, dict):
            raise ValueError("RunState.from_json: payload must be a JSON object")

        status = data.get("status", "running")
        if status not in (
            "running",
            "paused_for_approval",
            "paused_for_input",
            "completed",
            "failed",
        ):
            raise ValueError(f"RunState.from_json: invalid status {status!r}")

        pause_reason_raw = data.get("pause_reason")
        pause_reason: PauseReason | None
        if pause_reason_raw is None:
            pause_reason = None
        else:
            try:
                pause_reason = PauseReason(pause_reason_raw)
            except ValueError as exc:
                raise ValueError(
                    f"RunState.from_json: invalid pause_reason {pause_reason_raw!r}"
                ) from exc

        paused_at_raw = data.get("paused_at")
        paused_at = (
            _deserialize_datetime(paused_at_raw) if paused_at_raw is not None else None
        )

        return cls(
            run_id=str(data["run_id"]),
            agent_name=str(data["agent_name"]),
            status=status,
            messages=[dict(m) for m in data.get("messages", [])],
            pending_tool_calls=[
                _deserialize_tool_call(tc) for tc in data.get("pending_tool_calls", [])
            ],
            approved_tool_call_ids=list(data.get("approved_tool_call_ids", [])),
            denied_tool_call_ids=list(data.get("denied_tool_call_ids", [])),
            started_at=_deserialize_datetime(data["started_at"]),
            paused_at=paused_at,
            pause_reason=pause_reason,
            deny_reasons=dict(data.get("deny_reasons", {})),
            final_output=data.get("final_output"),
            failure_reason=data.get("failure_reason"),
            metadata=dict(data.get("metadata", {})),
        )

    # ------------------------------------------------------------------
    # 便利 properties
    # ------------------------------------------------------------------

    @property
    def is_paused(self) -> bool:
        """是否處於 paused 狀態(approval / input 任一種皆視為 paused)。"""
        return self.status in ("paused_for_approval", "paused_for_input")

    @property
    def is_terminal(self) -> bool:
        """是否已到 terminal 狀態(completed / failed),不會再變化。"""
        return self.status in ("completed", "failed")

    def find_pending(self, call_id: str) -> ToolCall | None:
        """從 ``pending_tool_calls`` 找出指定 ``call_id`` 的 ToolCall。"""
        for tc in self.pending_tool_calls:
            if tc.call_id == call_id:
                return tc
        return None


# ---------------------------------------------------------------------------
# HITLController — pause / approve / deny / resume 主控制器
# ---------------------------------------------------------------------------


class HITLController:
    """HITL run state 操作 controller。

    五個核心動作:

    * :meth:`pause` — 把 running state 轉為 paused;依 reason 設定欄位。
    * :meth:`approve` — 把指定 call_id 從 pending 移到 approved。
    * :meth:`deny` — 把指定 call_id 從 pending 移到 denied,記下 reason。
    * :meth:`provide_input` — 把 paused_for_input 狀態下塞入 user message。
    * :meth:`resume` — 把 paused state 轉回 running(若所有 pending 都已處理)。

    本 controller 是 **stateless** — 只操作傳入的 ``RunState``;state 在
    caller 端保存(memory / file / Redis ...)。設計參考上游 ``RunState``
    的 instance method,但 ANILA 把 state 與行為分開,讓 RunState 保持
    pure dataclass(方便 dict-only round-trip)。
    """

    def pause(
        self,
        state: RunState,
        reason: PauseReason,
        *,
        pending_tool_calls: Iterable[ToolCall] | None = None,
        paused_at: datetime | None = None,
    ) -> RunState:
        """把 state 轉為 paused 狀態。

        Args:
            state: 當前 RunState。必須 ``status="running"``,否則 raise。
            reason: 進入暫停的原因(:class:`PauseReason`)。
            pending_tool_calls: 若 reason 為 ``TOOL_APPROVAL`` / ``GUARDRAIL_TRIPWIRE``,
                這裡帶 in-flight tool call。會 **取代** ``state.pending_tool_calls``。
            paused_at: 自訂時間;預設 ``_iso_now()``。

        Returns:
            **原** state instance(已就地修改;controller 保證 state identity 不變,
            方便 caller 用 single reference 跟蹤)。

        Raises:
            ValueError: state 不在 running 狀態。
        """
        if state.status != "running":
            raise ValueError(
                f"HITLController.pause: 只能從 running 進入 paused,目前 status={state.status!r}"
            )
        if reason == PauseReason.USER_INPUT_REQUIRED:
            state.status = "paused_for_input"
        else:
            state.status = "paused_for_approval"
        state.pause_reason = reason
        state.paused_at = paused_at or _iso_now()
        if pending_tool_calls is not None:
            state.pending_tool_calls = list(pending_tool_calls)
        return state

    def approve(self, state: RunState, call_id: str) -> RunState:
        """把指定 ``call_id`` 從 pending 移到 approved。

        Args:
            state: paused_for_approval 狀態的 RunState。
            call_id: 要 approve 的 tool call id。

        Returns:
            原 state(就地修改)。

        Raises:
            ValueError: 找不到對應 pending tool call。
        """
        tc = state.find_pending(call_id)
        if tc is None:
            raise ValueError(
                f"HITLController.approve: 找不到 pending tool call id={call_id!r}"
            )
        state.pending_tool_calls = [
            x for x in state.pending_tool_calls if x.call_id != call_id
        ]
        if call_id not in state.approved_tool_call_ids:
            state.approved_tool_call_ids.append(call_id)
        return state

    def deny(self, state: RunState, call_id: str, reason: str) -> RunState:
        """把指定 ``call_id`` 從 pending 移到 denied,並記下 reason。

        Args:
            state: paused_for_approval 狀態的 RunState。
            call_id: 要 deny 的 tool call id。
            reason: deny 理由(會存進 ``state.deny_reasons[call_id]``)。

        Returns:
            原 state(就地修改)。

        Raises:
            ValueError: 找不到對應 pending tool call。
        """
        tc = state.find_pending(call_id)
        if tc is None:
            raise ValueError(
                f"HITLController.deny: 找不到 pending tool call id={call_id!r}"
            )
        state.pending_tool_calls = [
            x for x in state.pending_tool_calls if x.call_id != call_id
        ]
        if call_id not in state.denied_tool_call_ids:
            state.denied_tool_call_ids.append(call_id)
        state.deny_reasons[call_id] = reason
        return state

    def provide_input(self, state: RunState, message: Message) -> RunState:
        """在 ``paused_for_input`` 狀態下塞入 user message。

        Args:
            state: paused_for_input 狀態的 RunState。
            message: 要附加到 ``state.messages`` 的 user message dict。

        Returns:
            原 state(就地修改);message 已 append。

        Raises:
            ValueError: state 不在 paused_for_input 狀態。
        """
        if state.status != "paused_for_input":
            raise ValueError(
                f"HITLController.provide_input: 只能在 paused_for_input 提供 input,"
                f"目前 status={state.status!r}"
            )
        state.messages.append(dict(message))
        return state

    def resume(self, state: RunState) -> RunState:
        """把 paused state 轉回 running。

        若仍有 ``pending_tool_calls``,**拒絕** resume — caller 必須先把所有
        pending 走完(approve 或 deny)。``paused_at`` / ``pause_reason``
        會被清為 None。

        Args:
            state: paused_for_approval / paused_for_input 狀態的 RunState。

        Returns:
            原 state(就地修改);status 改 running。

        Raises:
            ValueError: state 不在 paused 狀態,或仍有 pending tool call。
        """
        if not state.is_paused:
            raise ValueError(
                f"HITLController.resume: 只能從 paused 狀態 resume,目前 status={state.status!r}"
            )
        if state.pending_tool_calls:
            ids = [tc.call_id for tc in state.pending_tool_calls]
            raise ValueError(
                f"HITLController.resume: 仍有 pending tool calls={ids};請先 approve/deny。"
            )
        state.status = "running"
        state.pause_reason = None
        state.paused_at = None
        return state

    def complete(self, state: RunState, final_output: Any) -> RunState:
        """把 state 轉為 ``completed``,並寫入 ``final_output``。

        Args:
            state: running 狀態的 RunState。
            final_output: agent 最終輸出。

        Returns:
            原 state(就地修改)。

        Raises:
            ValueError: state 已在 terminal(completed / failed)。
        """
        if state.is_terminal:
            raise ValueError(
                f"HITLController.complete: state 已 terminal,status={state.status!r}"
            )
        state.status = "completed"
        state.final_output = final_output
        state.pause_reason = None
        state.paused_at = None
        return state

    def fail(self, state: RunState, reason: str) -> RunState:
        """把 state 轉為 ``failed``,並寫入 ``failure_reason``。

        Args:
            state: 任何 non-terminal 狀態的 RunState。
            reason: 失敗原因。

        Returns:
            原 state(就地修改)。

        Raises:
            ValueError: state 已 terminal。
        """
        if state.is_terminal:
            raise ValueError(
                f"HITLController.fail: state 已 terminal,status={state.status!r}"
            )
        state.status = "failed"
        state.failure_reason = reason
        return state


# ---------------------------------------------------------------------------
# JsonFileStateStore — 把 RunState 存成 .json 檔
# ---------------------------------------------------------------------------


class JsonFileStateStore:
    """把 :class:`RunState` 序列化成 .json 檔的最小 store。

    檔案命名規則:``<dir>/<run_id>.json``。

    設計取捨:

    * **不做 file lock** — ANILA P1-8 預設單 process / 單 thread。多 process
      請另外 wrap atomic rename + flock。
    * **deterministic save** — 用 :meth:`RunState.to_json` 寫入;同 state
      多次 save 產生相同 bytes(可拿來 diff)。
    * **error 風格** — load 失敗 raise :class:`FileNotFoundError`;parse
      失敗 raise :class:`ValueError`(讓 caller 區分「沒檔」與「壞檔」)。

    Args:
        dir: 存放 .json 的目錄。若不存在會在 ``__init__`` 自動建立。
    """

    def __init__(self, dir: Path) -> None:
        self._dir = Path(dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def dir(self) -> Path:
        """回傳目前的存放目錄(唯讀 view)。"""
        return self._dir

    def path_for(self, run_id: str) -> Path:
        """組出指定 run_id 對應的檔案路徑。"""
        # 防呆:run_id 不應含 path separator。
        if "/" in run_id or "\\" in run_id or run_id in ("", ".", ".."):
            raise ValueError(f"JsonFileStateStore: 不合法的 run_id={run_id!r}")
        return self._dir / f"{run_id}.json"

    def save(self, state: RunState) -> Path:
        """把 state 寫入 ``<dir>/<run_id>.json``。

        Args:
            state: 要存的 RunState。

        Returns:
            實際寫入的檔案路徑。
        """
        path = self.path_for(state.run_id)
        path.write_text(state.to_json(), encoding="utf-8")
        return path

    def load(self, run_id: str) -> RunState:
        """從 ``<dir>/<run_id>.json`` 讀回 RunState。

        Args:
            run_id: 要載入的 run id。

        Returns:
            還原後的 :class:`RunState`。

        Raises:
            FileNotFoundError: 對應檔案不存在。
            ValueError: 檔案內容 parse 失敗(JSON 壞或 schema 不符)。
        """
        path = self.path_for(run_id)
        if not path.exists():
            raise FileNotFoundError(
                f"JsonFileStateStore.load: run_id={run_id!r} 對應檔案不存在 ({path})"
            )
        return RunState.from_json(path.read_text(encoding="utf-8"))

    def delete(self, run_id: str) -> bool:
        """刪除 ``<dir>/<run_id>.json``。

        Args:
            run_id: 要刪除的 run id。

        Returns:
            True 表示確實刪除;False 表示原本就不存在(不視為錯誤)。
        """
        path = self.path_for(run_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def list_run_ids(self) -> list[str]:
        """列出目錄下所有 ``<run_id>.json`` 對應的 run_id(排序好)。"""
        return sorted(p.stem for p in self._dir.glob("*.json"))


# ---------------------------------------------------------------------------
# Streaming integration helper
# ---------------------------------------------------------------------------


def pause_streaming_if_needed(state: RunState | None) -> bool:
    """供 streaming runner 用的 helper:state 是否需要立刻停止 yield?

    P1-7 ``AnilaStreamRunner`` 在 yield ``final_output`` 之前可呼叫此函式;
    若回 True,runner 應 ``return`` 而不再 yield。

    Args:
        state: 可選的 RunState;為 ``None`` 表示沒接 HITL,直接 False。

    Returns:
        若 state 為 paused → True;否則 False。
    """
    if state is None:
        return False
    return state.is_paused


__all__ = [
    "HITLController",
    "JsonFileStateStore",
    "Message",
    "PauseReason",
    "RunState",
    "RunStatus",
    "pause_streaming_if_needed",
]
