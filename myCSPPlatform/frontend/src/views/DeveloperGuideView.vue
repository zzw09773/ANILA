<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="page-head__eyebrow">developer · guide</p>
        <h1 class="page-head__title">打造 ANILA agent</h1>
        <p class="page-head__sub">
          fork anila-agent · 包成 service · 註冊 · 上線到 router
        </p>
      </div>
    </header>

    <!-- TL;DR -->
    <TermBox title="tl;dr · 五步上線" pad="md">
      <ol class="tldr">
        <li>從 <router-link to="/developer/agents">/developer/agents</router-link> 下載 template (anila-agent 0.2.0 subtree snapshot)</li>
        <li><code>uv venv &amp;&amp; uv pip install -e '.[dev,pgvector]'</code> · <code>cp .env.example .env</code> · 填 <code>ANILA_BASE_URL</code> / <code>ANILA_API_KEY</code> / <code>ANILA_MODEL</code></li>
        <li>選 retriever (Dummy → langchain pgvector → ANILA-native pgvector) · 加你自己的 <code>@anila_tool</code> · 跑 <code>anila</code> 驗 REPL</li>
        <li>包一層 FastAPI 對外吐 <code>/health</code> + <code>/v1/chat/completions</code> + <code>/v1/models</code> (bridge openai-agents Runner ↔ OpenAI-compat SSE)</li>
        <li>到 <router-link to="/developer/agents">/developer/agents</router-link> 註冊 · 等管理員審核通過 · 拿 bootstrap token 換 service token · router 自動發現</li>
      </ol>
    </TermBox>

    <!-- Section nav -->
    <TermBox title="目錄" pad="sm">
      <ul class="toc">
        <li><a href="#what-you-fork">你 fork 到的是什麼</a></li>
        <li><a href="#quickstart">本地 quickstart · REPL</a></li>
        <li><a href="#retriever">retriever · 三種選擇</a></li>
        <li><a href="#tools">加工具 · @anila_tool</a></li>
        <li><a href="#hooks">hooks · 事件攔截</a></li>
        <li><a href="#memory">memory · 長短期 + 自動抽取</a></li>
        <li><a href="#fastapi">包 FastAPI service · 對外 OpenAI-compat</a></li>
        <li><a href="#platform-primitives">anila-core 平台 primitives</a></li>
        <li><a href="#endpoints">agent 必須暴露的端點</a></li>
        <li><a href="#bootstrap">註冊 · bootstrap · service token</a></li>
        <li><a href="#runtime-config">runtime_config · 不重啟調整</a></li>
        <li><a href="#testing">測試與品質閘門</a></li>
        <li><a href="#troubleshoot">疑難排解</a></li>
      </ul>
    </TermBox>

    <!-- What you fork -->
    <TermBox id="what-you-fork" title="你 fork 到的是什麼" pad="md">
      <p class="lead">
        ANILA Phase 2 把 sub-agent 模板拆成「runtime」+「平台 primitives」兩塊獨立演進:
      </p>
      <table class="term-table">
        <thead>
          <tr><th style="width: 160px">套件</th><th>用途</th><th>來源</th></tr>
        </thead>
        <tbody>
          <tr>
            <td><code>anila-agent</code></td>
            <td>
              你 fork 的<strong>主體</strong>。基於 <code>openai-agents</code> SDK 的 runtime,加上
              port 自 Claude Code 的 harness (memdir / hook / slash-command CLI)。
              提供 retriever Protocol + 3 個內建後端 (Dummy / langchain pgvector / ANILA-native pgvector)、
              <code>@anila_tool</code> 裝飾器、5 個 hook event。
            </td>
            <td>
              <code>github.com/zzw09773/anila-agent</code> ·
              CSP 內以 git subtree 收在 <code>./anila-agent</code> 並由 <code>/api/agents/template/download</code> 打包
            </td>
          </tr>
          <tr>
            <td><code>anila-core</code></td>
            <td>
              ANILA 平台特有的 primitive: <code>ToolDefinition</code> + permission / safety、
              <code>Workspace</code> 沙盒 + caps、guardrails (PII/regex 遮罩、長度上限)、
              <code>RuntimeConfigPoller</code> (跟 CSP 對拉 hot-reload 設定)、
              人機互動工具 (<code>ask_user</code> / <code>plan_mode</code> / <code>todo_write</code>)。
            </td>
            <td>repo 內 <code>./anila-core</code>; agent 端 <code>pip install ./anila-core</code></td>
          </tr>
        </tbody>
      </table>
      <p class="hint">
        <strong>分工原則:</strong> anila-agent 在 ANILA repo 外可獨立使用 (對外開源、純 openai-agents 生態);
        anila-core 是「跟 CSP 平台對話的協議層」,離開 ANILA 沒太大意義。
        把客製邏輯放在你自己的 fork — 兩個依賴都別改,以後才能 <code>git subtree pull</code> / <code>pip install -U</code> 拿上游更新。
      </p>
    </TermBox>

    <!-- Quickstart -->
    <TermBox id="quickstart" title="本地 quickstart · REPL" pad="md">
      <p>
        anila-agent 本身是個 CLI tool — 沒有內建 HTTP server,可以先用 REPL 把 retriever / 工具 / hook 流程跑通,再去做 FastAPI 包裝。
      </p>
      <pre class="code">cd anila-agent
uv venv &amp;&amp; source .venv/bin/activate
uv pip install -e '.[dev,pgvector]'   # pgvector extra 後面要用

cp .env.example .env
# 編輯 .env:
#   ANILA_BASE_URL=http://your-vllm:8000/v1
#   ANILA_API_KEY=sk-local
#   ANILA_MODEL=google/gemma4

# 跑 REPL
anila

# 或一次性跑 prompt
anila --prompt "what tools do you have?"</pre>
      <p>
        REPL 內的 slash command:
      </p>
      <table class="term-table">
        <thead><tr><th>command</th><th>效果</th></tr></thead>
        <tbody>
          <tr><td><code>/help</code></td><td>列出所有指令</td></tr>
          <tr><td><code>/clear</code></td><td>清掉這個 session 的短期記憶</td></tr>
          <tr><td><code>/memory list</code></td><td>顯示 MEMORY.md 索引</td></tr>
          <tr><td><code>/memory scan</code></td><td>顯示完整的 memory file manifest</td></tr>
          <tr><td><code>/memory extract</code></td><td>強制跑一次 auto extraction (前提是 <code>memory.yaml</code> 已開)</td></tr>
          <tr><td><code>/model</code></td><td>顯示目前 active model</td></tr>
          <tr><td><code>/cost</code></td><td>顯示這個 session 的 token / cost metrics</td></tr>
          <tr><td><code>/exit</code></td><td>離開</td></tr>
        </tbody>
      </table>
      <p class="hint">
        要加自己的 slash command: 進 <code>anila_agent/cli/commands.py</code>。
      </p>
    </TermBox>

    <!-- Retriever -->
    <TermBox id="retriever" title="retriever · 三種選擇" pad="md">
      <p class="lead">
        <code>build_agent()</code> 啟動時會依環境變數自動掛上對應 retriever。
        優先序 <strong>ANILA-native → langchain-postgres → DummyRetriever</strong>。
        半套配置 (例如 <code>ANILA_COLLECTION_ID</code> 設了但 <code>PGVECTOR_URL</code> 沒設) 會大聲 raise,不會偷偷退回 Dummy。
      </p>

      <h4>選項 A · DummyRetriever (預設)</h4>
      <p>
        in-memory token-overlap,適合在你還沒接資料庫前先把 agent 流程驗通。
        什麼都不設就是這個。
      </p>

      <h4>選項 B · 通用 pgvector (langchain schema)</h4>
      <p>
        資料若是用 langchain 的 <code>PGVector</code> 灌進去的 (<code>langchain_pg_collection</code> +
        <code>langchain_pg_embedding</code> 兩張表),設兩個 env 就好,零碼:
      </p>
      <pre class="code">PGVECTOR_URL=postgresql+psycopg2://user:pass@host:5432/db
PGVECTOR_COLLECTION=my_docs

# embed endpoint 預設 fallback 到 ANILA_BASE_URL / ANILA_API_KEY,
# 不同就獨立指定:
ANILA_EMBED_MODEL=text-embedding-3-small
# ANILA_EMBED_BASE_URL=...
# ANILA_EMBED_API_KEY=...</pre>

      <h4>選項 C · ANILA 平台 pgvector</h4>
      <p>
        資料若灌在 ANILA 平台的 <code>ingestion_collections</code> + <code>document_chunks</code>
        (halfvec + RLS via <code>anila.collection_id</code> GUC),用這個。
        embedding 維度會從 <code>ingestion_collections.embedding_dim</code> 自動抓,
        所以同一份 code 跑不同維度的 collection 都行。
      </p>
      <pre class="code">PGVECTOR_URL=postgresql://csp:csp@127.0.0.1:5433/csp
ANILA_COLLECTION_ID=52
ANILA_EMBED_MODEL=nvidia/NV-embed-V2
ANILA_SSL_VERIFY=0   # 只在 embed endpoint 用自簽憑證時才設</pre>

      <h4>選項 D · 自己實作 Retriever</h4>
      <p>
        Protocol 在 <code>anila_agent/retrieval/base.py</code>:
      </p>
      <pre class="code">from anila_agent.retrieval.base import Retriever
from anila_agent.models.schemas import Document

class MyRetriever:
    @property
    def name(self) -&gt; str:
        return "mine"

    async def search(self, query: str, k: int = 5) -&gt; list[Document]:
        ...

    async def fetch(self, doc_id: str) -&gt; Document | None:
        ...</pre>
      <p>建好之後在 agent 組裝前注入:</p>
      <pre class="code">from anila_agent.tools.rag_tools import set_retriever
set_retriever(MyRetriever())
# 內建的 search_documents / read_document tool 會自動走它。
# 完整範例:examples/rag_agent.py</pre>
    </TermBox>

    <!-- Tools -->
    <TermBox id="tools" title="加工具 · @anila_tool" pad="md">
      <p>
        <code>@anila_tool</code> 包了 openai-agents 的 <code>@function_tool</code>,
        多帶了 ANILA 的 metadata (<code>is_read_only</code> / <code>is_destructive</code> / <code>category</code>)
        — 這些 metadata 之後在 CSP 上做 tool permission UI 用得到。
        JSON schema 由 Python type hints + docstring 自動產生。
      </p>
      <pre class="code">from anila_agent.tools.base import anila_tool

@anila_tool(is_read_only=True, category="domain")
def employee_count(department: str) -&gt; int:
    """Count active employees in a department.

    Args:
        department: Department name, e.g. "Engineering".
    """
    return _query_hr_db(department)</pre>

      <h4>把工具註冊進 agent</h4>
      <p>兩種方式擇一:</p>
      <p><strong>1) 直接 import 到 agent assembly</strong> — 改 <code>anila_agent/core/agent.py</code> 或在 wrapper 端覆寫。</p>
      <p><strong>2) 列在 <code>configs/tools.yaml</code></strong> — 不動程式:</p>
      <pre class="code">builtin:
  - mypkg.tools.employee_count
  - mypkg.tools.list_open_tickets</pre>
      <p class="hint">
        完整可跑範例 → <code>examples/custom_tool.py</code>。
      </p>
    </TermBox>

    <!-- Hooks -->
    <TermBox id="hooks" title="hooks · 事件攔截" pad="md">
      <p>
        Hook 在 model 跟 tool 事件前後觸發,回 <code>HookOutput</code> 決定後續:
        <code>decision="block"</code> (擋掉)、修改 input、注入 context、純觀察。
      </p>
      <pre class="code">from anila_agent.core.hooks import HookOutput, PreToolUseInput

async def deny_writes(payload: PreToolUseInput) -&gt; HookOutput:
    if payload.tool_name.startswith("write_"):
        return HookOutput(decision="block", reason="read-only mode")
    return HookOutput()</pre>
      <p>在 <code>configs/tools.yaml</code> 註冊:</p>
      <pre class="code">hooks:
  pre_tool_use:
    - { matcher: "write_.*", callback: mypkg.hooks.deny_writes }</pre>

      <h4>可用事件</h4>
      <table class="term-table">
        <thead><tr><th>event</th><th>觸發時機</th><th>典型用途</th></tr></thead>
        <tbody>
          <tr>
            <td><code>pre_tool_use</code></td>
            <td>tool 呼叫前</td>
            <td>權限閘門、input redact、注入 context</td>
          </tr>
          <tr>
            <td><code>post_tool_use</code></td>
            <td>tool 回傳後</td>
            <td>output 觀察、結果裁切、下一輪 context 注入</td>
          </tr>
          <tr>
            <td><code>stop</code></td>
            <td>agent 給出 final output 時</td>
            <td>auto memory 抽取、稽核紀錄、cost 結算</td>
          </tr>
          <tr>
            <td><code>session_start</code></td>
            <td>session 建立時</td>
            <td>從外部讀取使用者 profile、預載 memory</td>
          </tr>
          <tr>
            <td><code>user_prompt_submit</code></td>
            <td>每次使用者送 prompt 進來</td>
            <td>prompt-injection 防守、PII 遮罩</td>
          </tr>
        </tbody>
      </table>
    </TermBox>

    <!-- Memory -->
    <TermBox id="memory" title="memory · 長短期 + 自動抽取" pad="md">
      <h4>長期 memory (memdir)</h4>
      <p>
        檔案存在 <code>&lt;ANILA_HOME&gt;/memory/</code>。布局:
      </p>
      <pre class="code">memory/
  MEMORY.md              ← 索引,上限 200 行 / 25 KB
  user_role.md           ← topic file,帶 YAML frontmatter
  feedback_testing.md
  project_release.md</pre>
      <p>每個 topic file:</p>
      <pre class="code">---
name: short title
description: 一行描述 · recall selector 拿來決定要不要回想
type: user|feedback|project|reference
---

free-form markdown 內容</pre>
      <p>
        Recall 流程:掃目錄、把 manifest 丟給一個小 LLM call、回傳被選中的檔案內容。
        架構直接 port 自 Claude Code 的 memdir。
      </p>

      <h4>自動抽取 (預設關閉)</h4>
      <p>
        要開的話編 <code>configs/memory.yaml</code>:
      </p>
      <pre class="code">auto_memory:
  enabled: true
  min_messages_between_runs: 4</pre>
      <p>
        開了之後每輪結束的 <code>stop</code> hook 會跑一次 extractor 的 side LLM call,
        把候選 memory 寫成新檔案。關著就是「每輪 cost 可預期」。
      </p>

      <h4>短期 memory</h4>
      <p>
        SQLite-backed,走 openai-agents 的 <code>SQLiteSession</code>,
        存在 <code>&lt;ANILA_HOME&gt;/sessions/anila.db</code>。
        同個 <code>--session</code> ID 再進 REPL 就會延續上次對話。
      </p>
    </TermBox>

    <!-- FastAPI wrapper -->
    <TermBox id="fastapi" title="包 FastAPI service · 對外 OpenAI-compat" pad="md">
      <p class="lead">
        <strong>這是 anila-agent 沒幫你做的關鍵一步。</strong>
        anila-agent 是 CLI / library — 要讓 CSP router 找得到,你得把
        <code>build_agent()</code> + <code>AnilaRunner</code> 包進一個小 FastAPI app,
        對外吐 OpenAI-compatible 的 endpoint。CSP 期待的三個端點都要實作 (細節看下一節)。
      </p>
      <pre class="code">from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

from anila_agent.core.agent import build_agent
from anila_agent.core.runner import AnilaRunner

_agent = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent
    _agent = build_agent()        # 讀 configs/ + env,掛 retriever / tools / hooks
    yield

app = FastAPI(lifespan=lifespan)

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [{"id": _agent.model, "object": "model", "owned_by": "anila"}],
    }

@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    body = await req.json()
    user_msg = body["messages"][-1]["content"]
    session_id = body.get("user") or body.get("session_id") or "anon"
    stream = body.get("stream", True)

    runner = AnilaRunner(_agent, session_id=session_id)

    if not stream:
        result = await runner.run(user_msg)
        return JSONResponse(_as_openai_response(result, _agent.model))

    async def sse():
        async for delta in runner.stream(user_msg):
            yield f"data: {_as_openai_chunk(delta, _agent.model)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream")</pre>
      <p class="hint">
        <code>_as_openai_response</code> / <code>_as_openai_chunk</code> 是你自己寫的 bridge
        (把 AnilaRunner 的 event 轉成 OpenAI <code>chat.completion.chunk</code> 形狀)。
        最小可工作版就只需要把 final text delta 包成 OpenAI <code>choices[0].delta.content</code>;
        要更花俏的 tool-call 事件可後續再補。
      </p>
      <p>
        Container 化建議 (跟 CSP entrypoint 對齊):
      </p>
      <pre class="code">FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
COPY anila_agent/ ./anila_agent/
RUN pip install -e '.[pgvector]' fastapi uvicorn
COPY configs/ ./configs/
COPY app.py .
ENV ANILA_HOME=/var/lib/anila-agent
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "24786"]</pre>
    </TermBox>

    <!-- Platform primitives -->
    <TermBox id="platform-primitives" title="anila-core 平台 primitives" pad="md">
      <p class="lead">
        想要更深的 CSP 整合 (集中式 tool permission、workspace 沙盒、guardrails、不重啟調參) — 把
        <code>anila-core</code> 裝進你的 fork:
      </p>
      <pre class="code">uv pip install -e /path/to/ANILA/anila-core
# 或在 pyproject.toml 加: anila-core @ file:///path/to/ANILA/anila-core</pre>

      <h4>tool permission · ALLOW / ASK / DENY</h4>
      <p>
        每個 <code>ToolDefinition</code> 帶 <code>permission</code> 跟 <code>safety</code>
        兩個獨立欄位。<code>ASK</code> 在 wrapper 端會生 <code>tool_approval</code> 中斷,
        等使用者授權後 bypass 那一次。<code>DENY</code> 直接拒。
      </p>
      <pre class="code">from anila_core.models.tool import ToolDefinition, ToolPermission, ToolSafety

dangerous = ToolDefinition(
    name="exec_python",
    description="執行 Python script",
    input_schema={"type": "object", "properties": {"code": {"type": "string"}}},
    safety=ToolSafety.DESTRUCTIVE,
    permission=ToolPermission.ASK,   # 👈 預設要使用者授權
    implementation=run_python,
)</pre>

      <h4>workspace · 沙盒目錄 + caps</h4>
      <p>
        <code>Workspace</code> 是個能力範圍化的暫存目錄。
        file / shell / python 工具都在 workspace 內跑,路徑跳脫一律擋。
        <code>WorkspaceCaps</code> 控制讀寫 / 網路 / subprocess / 大小上限 / 指令白名單。
      </p>
      <pre class="code">from anila_core.workspace import make_workspace
from anila_core.workspace.caps import WorkspaceCaps
from anila_core.tools.files import file_read, file_write, glob, grep
from anila_core.tools.shell import exec_bash, exec_python
from anila_core.tools.apply_patch import apply_patch

caps = WorkspaceCaps(
    fs_read=True, fs_write=True,
    network=False,             # 子 process 看不到代理 env
    exec_bash=True,
    command_allowlist=("ls", "cat", "grep", "rg"),
    max_exec_seconds=10,
    max_workspace_size_mb=50,
)
async with make_workspace("code-review", caps) as ws:
    # 把 ws 注進 hook 或工具的 context,
    # file_read / file_write / exec_bash 從 context 拿路徑。
    ...</pre>

      <h4>guardrails · 資料閘道</h4>
      <p>
        guardrails 跟 permission 是兩件事 — permission 管「能不能跑」,
        guardrails 管「資料能不能流」。
        三組內建 (regex block / max length) + Protocol 介面讓你寫自訂的。
      </p>
      <pre class="code">from anila_core.engine.guardrails import (
    RegexBlockInput, RegexBlockOutput, MaxLengthOutput,
)
from anila_core.models.tool import ToolDefinition

t = ToolDefinition(
    name="exec_python",
    description="...",
    input_schema={...},
    implementation=run_py,
    input_guardrails=[
        # 把疑似 API key 的 token 在送進工具前 redact 掉
        RegexBlockInput(
            pattern=r"sk-[a-zA-Z0-9]+",
            mode="redact",
            replacement="[REDACTED]",
        ),
        # 看到 password=xxx 直接 reject
        RegexBlockInput(pattern=r"password=\S+", mode="reject"),
    ],
    output_guardrails=[
        # 工具回傳給 model 前砍到 4096 字以內
        MaxLengthOutput(max_chars=4096),
        # 防止洩漏 .env 內容
        RegexBlockOutput(pattern=r"DATABASE_URL=\S+", mode="redact"),
    ],
)</pre>
      <p class="hint">
        <code>bypass_gates</code> (resume tool_approval 用) 會跳 permission /
        plan_mode 兩道閘門,但 <strong>guardrails 永遠跑</strong> — 資料清洗跟人授權無關。
      </p>

      <h4>人機互動工具</h4>
      <p>
        <code>anila_core.tools.ask_user</code> / <code>plan_mode</code> /
        <code>todo_write</code> 提供「暫停問人」「先確認再執行」「任務板」
        三組工具。把它們 wrap 成 <code>@anila_tool</code> 即可:
      </p>
      <pre class="code">from anila_core.tools.ask_user import ask_user as core_ask_user
from anila_agent.tools.base import anila_tool

@anila_tool(category="meta", is_read_only=True)
async def ask_user(question: str, options: list[str] | None = None) -&gt; str:
    """Pause and ask the user a question.

    Args:
        question: What to ask.
        options: Optional multiple-choice list.
    """
    return await core_ask_user(question=question, options=options or [])</pre>
      <p class="hint">
        anila-agent 0.2.0 目前還沒原生橋接這類「pause/resume」事件到外部 SSE — 你的 FastAPI wrapper 要自己決定
        怎麼把 <code>InterruptItem</code> 流出去 (例如自訂 SSE event <code>interrupt_requested</code>,
        前端的 <code>InterruptCard</code> / <code>TodoChecklist</code> / <code>PlanCard</code>
        已備好)。
      </p>
    </TermBox>

    <!-- Endpoints -->
    <TermBox id="endpoints" title="agent 必須暴露的端點" pad="md">
      <p>
        CSP router 期待這三個端點。前一節 (<a href="#fastapi">包 FastAPI service</a>) 的 boilerplate
        已經把骨架寫好。
      </p>
      <table class="term-table">
        <thead>
          <tr><th style="width: 70px">method</th><th>path</th><th>auth</th><th>用途</th></tr>
        </thead>
        <tbody>
          <tr>
            <td><code>GET</code></td><td><code>/health</code></td><td>public</td>
            <td>discovery + health probe,回 <code>{"status":"ok"}</code></td>
          </tr>
          <tr>
            <td><code>GET</code></td><td><code>/v1/models</code></td><td>s2s</td>
            <td>列出可用的 model id (OpenAI-compat)</td>
          </tr>
          <tr>
            <td><code>POST</code></td><td><code>/v1/chat/completions</code></td><td>s2s</td>
            <td>主推論 (OpenAI-compat);預設走 SSE stream</td>
          </tr>
        </tbody>
      </table>
      <p class="hint">
        s2s = service-to-service。兩個 auth header 並行驗:
        平台側帶 <code>X-CSP-Service-Token</code>、
        外部客戶端 (OpenWebUI 等) 用 <code>Authorization: Bearer ...</code>。
        anila-agent 沒幫你驗,要在 wrapper 加 dependency。
      </p>
    </TermBox>

    <!-- Bootstrap -->
    <TermBox id="bootstrap" title="註冊 · bootstrap · service token" pad="md">
      <ol class="steps">
        <li>
          到 <router-link to="/developer/agents">/developer/agents</router-link>
          下載 template (按 <strong>download template</strong>),把 anila-agent + 你客製的工具裝箱
        </li>
        <li>
          填好 agent 名稱 / endpoint URL / 底層模型,送出註冊 ·
          狀態起始為 <TermBadge variant="warn">pending</TermBadge>
        </li>
        <li>
          管理員 approve · 狀態翻成 <TermBadge variant="ok">approved</TermBadge>
        </li>
        <li>
          管理員核發一次性的 <strong>bootstrap token</strong>
          (<code>bsk-...</code>,15 分鐘 TTL)
        </li>
        <li>
          在你 agent 的 <code>.env</code> 設好:
          <pre class="code">CSP_URL=http://csp:8000
ANILA_AGENT_ID=&lt;your-id&gt;
ANILA_ENDPOINT_URL=http://&lt;your-host&gt;:24786
CSP_BOOTSTRAP_TOKEN=bsk-XXXX-from-admin</pre>
        </li>
        <li>
          第一次啟動時呼叫 CSP 的 <code>POST /api/agents/bootstrap</code>:
          用 <code>bsk-</code> 換長期 <code>csk-...</code> service token,
          寫到 <code>/var/lib/anila-agent/service_token.json</code> (mode 0600)
        </li>
        <li>
          從 <code>.env</code> 拿掉 <code>CSP_BOOTSTRAP_TOKEN</code> ·
          已經被消費掉了,CSP 會擋 replay
        </li>
        <li>
          router 自動探測你的 <code>/health</code> · 開始派送流量
        </li>
      </ol>
      <p class="hint">
        anila-agent 0.2.0 沒內建 bootstrap CLI — 自己寫個 ~30 行的 Python 在 entrypoint 跑,
        參考 CSP backend <code>POST /api/agents/bootstrap</code> 的 schema。
        Service token rotation / 多 replica 共用 fleet token 的策略由你決定。
      </p>
    </TermBox>

    <!-- runtime_config -->
    <TermBox id="runtime-config" title="runtime_config · 不重啟調整 agent" pad="md">
      <p class="lead">
        管理員在 CSP 改 permission / workspace caps / guardrails,
        agent 程序每 30 秒輪詢 <code>GET /api/agents/me/runtime-config</code>,
        下一輪自動套用。沒重啟、沒 redeploy。
      </p>
      <p>
        agent 端要做的事:在 FastAPI <code>lifespan</code> 啟動
        <code>RuntimeConfigPoller</code>,把它指向你的 <code>ToolRegistry</code>:
      </p>
      <pre class="code">from contextlib import asynccontextmanager
from fastapi import FastAPI
from anila_core.config import settings
from anila_core.runtime_config import RuntimeConfigPoller
from anila_core.workspace.caps import WorkspaceCaps

@asynccontextmanager
async def lifespan(app: FastAPI):
    poller = RuntimeConfigPoller(
        csp_base_url=settings.csp_base_url,
        csp_service_token=settings.csp_service_token,
        registry=tool_registry,
        base_workspace_caps=WorkspaceCaps(),   # 你 agent 的預設值
        on_change=lambda snap, caps: workspace_factory.update_caps(caps),
        interval_seconds=30,
    )
    await poller.start()       # 第一次 poll 是 inline,所以 lifespan 結束時 caps 已套好
    try:
        yield
    finally:
        await poller.stop()</pre>
      <p>
        管理介面 → <router-link to="/developer/agents">/developer/agents</router-link>
        點 agent detail → <code>edit runtime config</code>。
        三個分頁:<strong>tool permissions</strong> /
        <strong>workspace caps</strong> /
        <strong>guardrails</strong>。
        存檔後 agent 在 30 秒內套用。
      </p>
      <p class="hint">
        ETag short-circuit:CSP 算出來的 hash 跟上次一樣就直接跳過 apply,
        不會在每次 poll 都重建 guardrail 物件。
        失敗 (4xx / 5xx / 連線錯誤) 不會清掉現行 snapshot,agent 維持上次成功的設定。
      </p>
    </TermBox>

    <!-- Testing -->
    <TermBox id="testing" title="測試與品質閘門" pad="md">
      <pre class="code">cd anila-agent
uv pip install -e '.[dev,pgvector]'
pytest                                  # 65 tests in 0.2.0
pytest --cov=anila_agent --cov-report=term-missing
mypy anila_agent/                       # strict mode
ruff check anila_agent/ tests/</pre>
      <p>
        anila-agent 0.2.0 維持的品質基線:65 tests all green、harness 層 (memdir port / hook bridge /
        retriever scoring) coverage 集中。
        openai-agents primitive 不在這份 coverage 範圍 — 上游自己測。
      </p>
      <p class="hint">
        新工具請在 <code>tests/</code> 下加測試 — <code>tests/test_retriever.py</code> 是
        Protocol 覆蓋的好範本,<code>tests/test_pgvector_retriever.py</code> /
        <code>test_anila_pgvector_retriever.py</code> 示範了兩個內建後端的 unit 測法。
      </p>
    </TermBox>

    <!-- Troubleshooting -->
    <TermBox id="troubleshoot" title="疑難排解" pad="md">
      <table class="term-table">
        <thead><tr><th>症狀</th><th>可能原因 · 修法</th></tr></thead>
        <tbody>
          <tr>
            <td><code>anila</code> CLI 起來就 <code>FileNotFoundError: prompts/system.md</code></td>
            <td>0.2.0 已修;舊版 <code>configs/agent.yaml</code> 指向相對路徑而 system.md 在 <code>anila_agent/prompts/</code> ·
              <code>git subtree pull</code> 升到 0.2.0 即可</td>
          </tr>
          <tr>
            <td>啟動 raise <code>"PGVECTOR_URL set but PGVECTOR_COLLECTION missing"</code></td>
            <td>0.2.0 對半套配置 fail loud (不再悄悄退回 Dummy) · 補齊兩個 env,
              或兩個都拿掉走 Dummy</td>
          </tr>
          <tr>
            <td><code>search_documents</code> 都回空</td>
            <td>collection 沒指派 · ingestion 還沒跑完 ·
              embedding model 跟灌資料時用的不一致 ·
              <code>ANILA_COLLECTION_ID</code> 指到空 collection</td>
          </tr>
          <tr>
            <td>vLLM endpoint 走自簽憑證連不上</td>
            <td>設 <code>ANILA_SSL_VERIFY=0</code> ·
              <strong>只在內網自簽情境用</strong>,公網一律別關</td>
          </tr>
          <tr>
            <td>memory 一直回空</td>
            <td><code>&lt;ANILA_HOME&gt;/memory/MEMORY.md</code> 是空的 ·
              手動加 topic file 或先讓 <code>auto_memory</code> 跑幾輪</td>
          </tr>
          <tr>
            <td>auto extraction 噴 token</td>
            <td>調大 <code>memory.yaml</code> 的 <code>min_messages_between_runs</code>,
              或乾脆把 <code>auto_memory.enabled</code> 設 <code>false</code> 改手動 <code>/memory extract</code></td>
          </tr>
          <tr>
            <td>CSP router 探不到你的 agent</td>
            <td>審核還是 <code>pending</code> ·
              管理員要到 <router-link to="/developer/agents">/developer/agents</router-link> approve ·
              或 <code>/health</code> wrapper 沒實作 / 回非 200</td>
          </tr>
          <tr>
            <td>bootstrap 噴 <code>token consumed</code></td>
            <td>service-token state 檔已經存在 · 想重新 bootstrap 就刪
              <code>/var/lib/anila-agent/service_token.json</code></td>
          </tr>
          <tr>
            <td>runtime_config 改了 agent 不動</td>
            <td>poller 沒掛 (lifespan 沒啟動) · CSP service token 失效 ·
              查 agent 端 log <code>RuntimeConfigPoller: 401/403</code></td>
          </tr>
        </tbody>
      </table>
    </TermBox>

    <!-- Footer cross-link -->
    <TermBox title="後續步驟" pad="md">
      <ul class="next">
        <li>下載 template / 註冊 agent → <router-link to="/developer/agents">/developer/agents</router-link></li>
        <li>瀏覽知識庫 collection → <router-link to="/knowledge-collections">/knowledge-collections</router-link></li>
        <li>anila-agent 上游 (含 CHANGELOG):
          <code>github.com/zzw09773/anila-agent</code></li>
        <li>本 repo 內 anila-agent subtree 更新指令見 ANILA <code>README.md</code> §「維護 anila-agent」</li>
      </ul>
    </TermBox>
  </div>
</template>

<script setup>
import { TermBox, TermBadge } from '../components/cli'
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); }
.page-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--gap-4); }
.page-head__eyebrow { color: var(--c-fg-mute); font-size: var(--t-xs); text-transform: uppercase; letter-spacing: 0.08em; margin: 0 0 4px; }
.page-head__title { font-size: var(--t-xl); font-weight: 500; color: var(--c-fg-1); margin: 0 0 4px; }
.page-head__sub { color: var(--c-fg-2); font-size: var(--t-sm); margin: 0; }

.lead { font-size: var(--t-sm); color: var(--c-fg-2); margin: 0 0 var(--gap-3); }
.lead strong { color: var(--c-fg-1); }

.tldr {
  list-style: decimal inside; padding: 0; margin: 0;
  display: flex; flex-direction: column; gap: var(--gap-2);
  font-size: var(--t-sm); color: var(--c-fg-2);
}
.tldr code {
  font-family: var(--font-mono); background: var(--c-bg);
  border: var(--border-w) solid var(--c-border); padding: 1px 4px;
  font-size: var(--t-2xs); color: var(--c-accent);
}

.toc {
  list-style: none; padding: 0; margin: 0;
  display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: var(--gap-2);
}
.toc a {
  color: var(--c-accent); text-decoration: none;
  font-family: var(--font-mono); font-size: var(--t-xs);
}
.toc a:hover { text-decoration: underline; }

h4 {
  font-size: var(--t-sm); color: var(--c-fg-1); font-weight: 500;
  margin: var(--gap-3) 0 var(--gap-2); letter-spacing: 0.02em;
}

.code {
  margin: var(--gap-2) 0; padding: var(--gap-3);
  background: var(--c-bg); border: var(--border-w) solid var(--c-border);
  font-family: var(--font-mono); font-size: var(--t-2xs);
  color: var(--c-fg-1); white-space: pre; overflow-x: auto;
  line-height: 1.55;
}

.hint {
  font-size: var(--t-xs); color: var(--c-fg-3); margin: 4px 0 0;
  font-style: italic;
}

.steps {
  list-style: decimal inside; padding: 0; margin: 0;
  display: flex; flex-direction: column; gap: var(--gap-2);
  font-size: var(--t-sm); color: var(--c-fg-2);
}
.steps strong { color: var(--c-fg-1); }
.steps code {
  font-family: var(--font-mono); background: var(--c-bg);
  border: var(--border-w) solid var(--c-border); padding: 1px 4px;
  font-size: var(--t-2xs); color: var(--c-accent);
}
.steps .code {
  margin-left: var(--gap-4);
  font-size: var(--t-2xs);
}

.next {
  list-style: none; padding: 0; margin: 0;
  display: flex; flex-direction: column; gap: var(--gap-2);
  font-size: var(--t-sm); color: var(--c-fg-2);
}
.next code {
  font-family: var(--font-mono); background: var(--c-bg);
  border: var(--border-w) solid var(--c-border); padding: 1px 4px;
  font-size: var(--t-2xs); color: var(--c-accent);
}
.next a { color: var(--c-accent); text-decoration: none; }
.next a:hover { text-decoration: underline; }

.term-table { width: 100%; }
.term-table code {
  font-family: var(--font-mono); background: var(--c-bg);
  border: var(--border-w) solid var(--c-border); padding: 1px 4px;
  font-size: var(--t-2xs); color: var(--c-accent);
}
</style>
