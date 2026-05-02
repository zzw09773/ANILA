<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="page-head__eyebrow">developer · guide</p>
        <h1 class="page-head__title">打造 ANILA agent</h1>
        <p class="page-head__sub">
          fork AgenticRAG · 寫你的工具 · 註冊 · 上線到 router
        </p>
      </div>
    </header>

    <!-- TL;DR -->
    <TermBox title="tl;dr · 五步上線" pad="md">
      <ol class="tldr">
        <li><code>git clone &lt;your-fork&gt;/AgenticRAG &amp;&amp; cd AgenticRAG</code></li>
        <li><code>cp .env.example .env</code> · 填 <code>LLM_URL</code> / <code>EMBEDDING_URL</code> / <code>DATABASE_URL</code></li>
        <li><code>docker compose up -d</code> · <code>curl :24786/health</code> → <code>{"status":"ok"}</code></li>
        <li>加你自己的工具（<code>@tool</code> 裝飾器、Markdown skill、或外部 MCP server） · 把客製邏輯放到 <code>agentic_rag/tools/</code></li>
        <li>到 <router-link to="/developer/agents">/developer/agents</router-link> 註冊 · 等管理員審核通過 · router 自動發現</li>
      </ol>
    </TermBox>

    <!-- Section nav -->
    <TermBox title="目錄" pad="sm">
      <ul class="toc">
        <li><a href="#what-you-fork">你 fork 到的是什麼</a></li>
        <li><a href="#tools">加工具 · @tool 裝飾器</a></li>
        <li><a href="#middleware">middleware · trace / cost / guardrail / retry</a></li>
        <li><a href="#advanced">進階 · coordinator · bg task · skills · mcp</a></li>
        <li><a href="#endpoints">agent 必須暴露的端點</a></li>
        <li><a href="#bootstrap">註冊 · bootstrap · service token</a></li>
        <li><a href="#testing">測試與品質閘門</a></li>
        <li><a href="#troubleshoot">疑難排解</a></li>
      </ul>
    </TermBox>

    <!-- What you fork -->
    <TermBox id="what-you-fork" title="你 fork 到的是什麼" pad="md">
      <p class="lead">
        <strong>AgenticRAG</strong> 是 ANILA Phase 1 的官方 sub-agent 模板。
        完全自包：對 ANILA 內部套件零依賴。
        第三方開源套件（langchain、llama-index、sentence-transformers …）隨你用 — 只是別把 anila-* 內部套件拉進來。
      </p>
      <p>
        Repo 內建一份 vendored 的 agent runtime 放在 <code>agentic_rag/runtime/framework/</code>
        （47 個模組，涵蓋 Action / Agent / Runner / Middleware / StateMachine / Memory / Coordinator / BG&nbsp;Task / Skill / MCP），
        還附上完整 RAG 管線（vector_search / keyword_search / read_document、階層式 chunker、含視覺的 ingestion、cross-encoder reranker）。
      </p>
      <p>
        並存兩個端點：<code>/chat</code>（舊的 QueryEngine，7-stage turn loop）跟 <code>/agentic-chat</code>（新的 framework Runner）。
        兩者吐相同的 SSE wire format。新 fork 應該瞄準 <code>/agentic-chat</code>。
      </p>
    </TermBox>

    <!-- Tools -->
    <TermBox id="tools" title="加工具 · @tool 裝飾器" pad="md">
      <p>
        <code>@tool</code> 裝飾器會從 Python type hints + Google 風格 docstring 的 <code>Args:</code> 區塊自動產生 JSON schema。
        把函式丟到 <code>agentic_rag/tools/</code>（或任何模組）後，串到你的 Agent 的 <code>actions=</code> tuple 即可。
      </p>
      <pre class="code">from agentic_rag.runtime.framework import tool, ActionContext
from typing import Annotated

@tool
async def get_weather(
    ctx: ActionContext,
    city: str,
    units: Annotated[str, "celsius or fahrenheit"] = "celsius",
) -&gt; dict:
    """Look up current weather for a city.

    Args:
        city: City name (e.g. "Taipei").
        units: Temperature unit.
    """
    # ... your retrieval / API logic here ...
    return {"city": city, "temp": 24, "units": units}</pre>
      <p class="hint">
        框架會自動把回傳的 dict 包成 <code>ActionResult(output=dict)</code>；想表達失敗就 raise 或 <code>return ActionResult(error="...")</code>。
        第一個參數必須是 <code>ctx</code> / <code>context</code> 或標註成 <code>ActionContext</code>。
      </p>

      <h4>串進 agent</h4>
      <pre class="code">from agentic_rag.runtime.framework import Agent, Runner
from agentic_rag.runtime.bridge import FrameworkProviderAdapter

adapter = FrameworkProviderAdapter(my_existing_provider)
agent = Agent(
    name="weather-bot",
    instructions="Use get_weather to answer weather questions.",
    provider=adapter,
    model="google/gemma4",
    actions=(get_weather,),
)
result = await Runner().run(agent, "What's the weather in Taipei?")
print(result.final_output)</pre>

      <h4>RAG 工具（已內建）</h4>
      <pre class="code">from agentic_rag.runtime.bridge import build_rag_agent

agent = build_rag_agent(
    name="rag-bot",
    instructions="Answer questions using the search tool.",
    provider=adapter,
    model="google/gemma4",
    store=my_pgvector_store,
    embedder=my_embed_fn,
    reranker=my_reranker,    # optional
)
# 自動註冊 vector_search / keyword_search / read_document。
# 每筆結果都是 Citation（chunk_id / document_title / heading_path / page / confidence）。</pre>
    </TermBox>

    <!-- Middleware -->
    <TermBox id="middleware" title="middleware · 6 個內建中件" pad="md">
      <p>
        Middleware 把每次 Action 呼叫包起來。Run-level middleware 包住 action-level middleware 再包住 handler。
        順序很重要：最先註冊的 = 最外層（最先看到 input，最後看到 output）。
      </p>
      <table class="term-table">
        <thead>
          <tr><th>middleware</th><th>做什麼</th><th>典型用途</th></tr>
        </thead>
        <tbody>
          <tr>
            <td><code>TraceMiddleware</code></td>
            <td>每個 action 開一個 span，記錄 input / output / 耗時</td>
            <td>audit log；即時 tracing dashboard</td>
          </tr>
          <tr>
            <td><code>CostMiddleware</code></td>
            <td>token 計數；選配 dollar 追蹤；budget gate</td>
            <td>各 agent 的容量報表（ANILA 純本地，沒有美金成本要算）</td>
          </tr>
          <tr>
            <td><code>GuardrailMiddleware</code></td>
            <td>input / output 檢查；allow / deny / modify 決策</td>
            <td>PII 遮罩；citation 強制；prompt-injection 防守</td>
          </tr>
          <tr>
            <td><code>RetryMiddleware</code></td>
            <td>對例外或 error result 做指數退避重試</td>
            <td>不穩定的 reranker；vLLM 抖動回復</td>
          </tr>
          <tr>
            <td><code>ShellHookMiddleware</code></td>
            <td>action 前後 spawn shell 命令；用 stdin/stdout 走 JSON</td>
            <td>企業 audit pipe；資安團隊維護的 deny-list</td>
          </tr>
          <tr>
            <td><code>ToolOutputTrimmerMiddleware</code></td>
            <td>限制 tool 輸出大小；超過門檻就替換成預覽</td>
            <td>vector_search 回 50 個 chunk 時不要把 context window 灌爆</td>
          </tr>
        </tbody>
      </table>

      <h4>範例 · 建一個帶 trace + retry 的 runner</h4>
      <pre class="code">from agentic_rag.runtime.framework import Runner
from agentic_rag.runtime.framework.middleware import (
    TraceMiddleware, InMemoryBackend, RetryMiddleware, RetryPolicy,
)

backend = InMemoryBackend()
runner = Runner(middleware=[
    TraceMiddleware(backend),
    RetryMiddleware(
        policy=RetryPolicy(max_attempts=3, initial_delay_seconds=0.25),
        on_exceptions=(ConnectionError,),
    ),
])
result = await runner.run(agent, "...")
# backend.spans now holds one Span per action invocation.</pre>

      <h4>citation guardrail · 強制答案 cite 來源</h4>
      <pre class="code">from agentic_rag.runtime.bridge import enforce_citations

result = await runner.run(agent, "what does the docs say about X?")
verdict = enforce_citations(result, mode="warn")  # mode="block" 則會直接 raise
# verdict.cited / verdict.matched / verdict.candidates</pre>
    </TermBox>

    <!-- Advanced primitives -->
    <TermBox id="advanced" title="進階 primitives" pad="md">
      <h4>Coordinator · LLM 驅動的 sub-agent fan-out</h4>
      <p>
        Spawn N 個平行的 sub-agent（read-only）或序列的（write-safe）。
        Coordinator <em>agent</em> 本身就是個普通 Agent，只是它的 <code>actions=</code> 帶了 <code>spawn_worker</code> / <code>check_worker</code> / <code>wait_for_workers</code>。
      </p>
      <pre class="code">from agentic_rag.runtime.framework import Coordinator, make_coordinator_actions

coord = Coordinator(workers={"verifier": verifier_agent, "summariser": summ_agent})
coord_agent = Agent(
    name="orchestrator",
    instructions="Decompose the request. spawn_worker for each subtask. wait_for_workers. summarise.",
    provider=adapter, model="google/gemma4",
    actions=tuple(make_coordinator_actions(coord)),
)</pre>

      <h4>BG_TASK · 長時間背景工作</h4>
      <p>
        給非 LLM 的批次工作用（ingest 一萬份 PDF、重建向量索引、批次推論）。
        立刻回 handle；LLM 用 <code>check_bg_task</code> / <code>cancel_bg_task</code> 控制。
      </p>
      <pre class="code">from agentic_rag.runtime.framework import (
    Action, ActionKind, BgTaskRunner, make_bg_task_actions,
)

async def ingest_corpus(ctx, write_progress):
    for i, doc in enumerate(load_corpus()):
        write_progress(f"processing {i}: {doc.path}\n")
        await index(doc)
    return {"docs_indexed": i + 1}

ingest_action = Action(
    name="ingest_corpus", description="Bulk-ingest a corpus folder.",
    kind=ActionKind.BG_TASK, handler=ingest_corpus,
)
bg_runner = BgTaskRunner(output_dir="/var/agent/bg")
runner = Runner(bg_task_runner=bg_runner)
agent = Agent(
    name="ops", instructions="...", provider=adapter, model="google/gemma4",
    actions=(ingest_action, *make_bg_task_actions(bg_runner)),
)</pre>

      <h4>Skills · Markdown frontmatter → tool</h4>
      <p>
        丟一個 <code>.md</code> 檔到 <code>~/.agentic-rag/skills/</code>；非工程師也能寫工具。
      </p>
      <pre class="code">---
name: summarise_pr
description: Summarise a GitHub PR for the user.
when_to_use: User asks "what's in this PR" or pastes a PR URL.
input_schema:
  type: object
  properties:
    url: {type: string, description: PR URL}
  required: [url]
---
You are summarising the GitHub pull request at {{ url }}.

Step 1: fetch the PR diff.
Step 2: identify the top three changes by impact.
Step 3: write a 3-bullet summary the user can scan.</pre>
      <pre class="code">from agentic_rag.runtime.framework import load_skills_from_dir, SkillRegistry

skills = load_skills_from_dir("~/.agentic-rag/skills/")
registry = SkillRegistry(skills)
# 全部 skill：
agent = Agent(actions=tuple(registry.all_actions()), ...)
# 或按 query 篩選（省 prompt token）：
relevant = registry.actions_for(user_query, limit=5)</pre>

      <h4>MCP · 接第三方 MCP server</h4>
      <p>
        接 <code>mcp-server-filesystem</code>、<code>mcp-server-github</code>、<code>mcp-server-sentry</code>，或任何自製 stdio MCP server。
        需要 <code>pip install 'agentic-rag[mcp]'</code>。
      </p>
      <pre class="code">from agentic_rag.runtime.framework.mcp import MCPClientPool, MCPServer

pool = MCPClientPool([
    MCPServer(name="fs", command="mcp-server-filesystem", args=["/data"]),
    MCPServer(name="gh", command="mcp-server-github"),
])
async with pool:
    agent = Agent(
        name="ops", instructions="...", provider=adapter, model="google/gemma4",
        actions=tuple(pool.all_actions()),  # tool names: fs__read_file, gh__list_prs, ...
    )</pre>
    </TermBox>

    <!-- Endpoints -->
    <TermBox id="endpoints" title="agent 必須暴露的端點" pad="md">
      <p>CSP router 會打這些端點。AgenticRAG 內的 <code>api.py</code> 已經實作好了。</p>
      <table class="term-table">
        <thead>
          <tr><th style="width: 70px">method</th><th>path</th><th>auth</th><th>用途</th></tr>
        </thead>
        <tbody>
          <tr>
            <td><code>GET</code></td><td><code>/health</code></td><td>public</td>
            <td>discovery + health probe，回 <code>{"status":"ok"}</code></td>
          </tr>
          <tr>
            <td><code>GET</code></td><td><code>/v1/models</code></td><td>s2s</td>
            <td>列出可用的 model id（OpenAI-compat）</td>
          </tr>
          <tr>
            <td><code>POST</code></td><td><code>/v1/chat/completions</code></td><td>s2s</td>
            <td>主推論（OpenAI-compat）；預設走 SSE stream</td>
          </tr>
          <tr>
            <td><code>POST</code></td><td><code>/agentic-chat</code></td><td>s2s</td>
            <td>framework Runner 的 SSE stream（更豐富的 tool-call 事件）</td>
          </tr>
          <tr>
            <td><code>POST</code></td><td><code>/documents/upload</code></td><td>s2s</td>
            <td>RAG 文件 ingestion；multipart/form-data</td>
          </tr>
          <tr>
            <td><code>POST</code></td><td><code>/search</code></td><td>s2s</td>
            <td>純檢索（不過 LLM）</td>
          </tr>
        </tbody>
      </table>
      <p class="hint">
        s2s = service-to-service。兩個 auth header 並行：平台來的 <code>X-CSP-Service-Token</code>、直連客戶端（OpenWebUI 等）用的 <code>Authorization: Bearer ...</code>。
      </p>
    </TermBox>

    <!-- Bootstrap -->
    <TermBox id="bootstrap" title="註冊 · bootstrap · service token" pad="md">
      <ol class="steps">
        <li>
          到 <router-link to="/developer/agents">/developer/agents</router-link> 註冊你的 agent ·
          狀態起始為 <TermBadge variant="warn">pending</TermBadge>
        </li>
        <li>
          管理員 approve · 狀態翻成 <TermBadge variant="ok">approved</TermBadge>
        </li>
        <li>
          管理員核發一次性的 <strong>bootstrap token</strong>（<code>bsk-...</code>，15 分鐘 TTL）
        </li>
        <li>
          在你 agent 的 <code>.env</code> 設好：
          <pre class="code">CSP_URL=http://csp:8000
ANILA_AGENT_ID=&lt;your-id&gt;
ANILA_ENDPOINT_URL=http://&lt;your-host&gt;:24786
CSP_BOOTSTRAP_TOKEN=bsk-XXXX-from-admin</pre>
        </li>
        <li>
          第一次 <code>docker compose up -d</code> 時 entrypoint 會自動跑 bootstrap CLI：把 bsk 換成長期的 <code>csk-...</code> service token，寫到 <code>/var/lib/anila-agent/service_token.json</code>（mode 0600）
        </li>
        <li>
          從 <code>.env</code> 拿掉 <code>CSP_BOOTSTRAP_TOKEN</code> · 已經被消費掉了，CSP 會擋 replay
        </li>
        <li>
          router 自動探測你的 <code>/health</code> · 開始派送流量
        </li>
      </ol>
      <p class="hint">
        完整生命週期（rotation、多 replica K8s、退回 fleet 共用 <code>CSP_SERVICE_TOKEN</code>）：見 <code>AgenticRAG/docs/BOOTSTRAP_DEPLOYMENT.md</code>
      </p>
    </TermBox>

    <!-- Testing -->
    <TermBox id="testing" title="測試與品質閘門" pad="md">
      <pre class="code">pip install -e '.[rag,dev]'
pytest                                    # 632 tests
pytest --cov=agentic_rag --cov-report=term-missing
mypy src/agentic_rag/runtime/             # strict mode
ruff check src/ tests/</pre>
      <p>
        Framework path 維持的品質基線：mypy strict + ruff clean + boundary test 確保不會漏進 anila-* 的硬依賴。
        新工具請在 <code>tests/</code> 下加測試 — <code>tests/runtime/framework/test_schema_generator.py</code> 是 <code>@tool</code> 覆蓋率的好範本。
      </p>
    </TermBox>

    <!-- Troubleshooting -->
    <TermBox id="troubleshoot" title="疑難排解" pad="md">
      <table class="term-table">
        <thead><tr><th>症狀</th><th>可能原因 · 修法</th></tr></thead>
        <tbody>
          <tr>
            <td>每個請求都回 <code>503</code></td>
            <td><code>API_KEY</code> 沒設且 <code>API_DEV_MODE</code> 不是 <code>true</code> · 在 <code>.env</code> 設其中一個</td>
          </tr>
          <tr>
            <td>bootstrap CLI 噴 <code>token consumed</code></td>
            <td>service-token state 檔已經存在 · 想重新 bootstrap 就刪掉 <code>/var/lib/anila-agent/service_token.json</code></td>
          </tr>
          <tr>
            <td>router 不派送流量到你的 agent</td>
            <td>審核還是 <code>pending</code> · 管理員要去 <router-link to="/developer/agents">/developer/agents</router-link> 處理</td>
          </tr>
          <tr>
            <td>vector_search 都回空</td>
            <td>collection 沒指派 · ingestion 還沒跑完 · embedding model 對不上（要跟 ingest 時用的一致）</td>
          </tr>
          <tr>
            <td>LLM 一直幻想出不存在的 tool name</td>
            <td>schema 沒送到 model · 檢查 <code>agent.registry.tool_definitions()</code> 輸出跟 provider 的預期是否一致</td>
          </tr>
          <tr>
            <td>pod 重啟後 memory extraction 大爆發</td>
            <td>把 <code>CursorStore</code> 設定到 <code>MemoryExtractor</code> 上做持久化 · 見 <code>agentic_rag/memory/extraction_state.py</code></td>
          </tr>
          <tr>
            <td>tool 輸出把 context window 灌爆</td>
            <td>在 runner middleware 加一個 <code>ToolOutputTrimmerMiddleware(max_chars=2000)</code></td>
          </tr>
          <tr>
            <td>取消訊號發出去但 run 沒停</td>
            <td>handler 必須在 await 點檢查 <code>ctx.metadata["_bg_cancel_signal"].is_set()</code>；框架沒辦法強制中斷任意 CPU 迴圈</td>
          </tr>
        </tbody>
      </table>
    </TermBox>

    <!-- Footer cross-link -->
    <TermBox title="後續步驟" pad="md">
      <ul class="next">
        <li>註冊你的 agent → <router-link to="/developer/agents">/developer/agents</router-link></li>
        <li>瀏覽知識庫 collection → <router-link to="/knowledge-collections">/knowledge-collections</router-link></li>
        <li>更深入的文件在 <code>AgenticRAG/docs/</code> · <code>BOOTSTRAP_DEPLOYMENT.md</code> · <code>CSP_INTEGRATION.md</code></li>
        <li>v0.1 milestone 變更紀錄：見 <code>AgenticRAG/README.md</code></li>
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
