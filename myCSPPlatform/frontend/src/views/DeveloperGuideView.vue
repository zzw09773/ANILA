<template>
  <div class="page">
    <header class="page-head">
      <div>
        <p class="page-head__eyebrow">developer · guide</p>
        <h1 class="page-head__title">build an ANILA agent</h1>
        <p class="page-head__sub">
          fork AgenticRAG · plug in your tools · register · ship to router
        </p>
      </div>
    </header>

    <!-- TL;DR -->
    <TermBox title="tl;dr" pad="md">
      <ol class="tldr">
        <li><code>git clone &lt;your-fork&gt;/AgenticRAG &amp;&amp; cd AgenticRAG</code></li>
        <li><code>cp .env.example .env</code> · fill <code>LLM_URL</code> / <code>EMBEDDING_URL</code> / <code>DATABASE_URL</code></li>
        <li><code>docker compose up -d</code> · <code>curl :24786/health</code> → <code>{"status":"ok"}</code></li>
        <li>add tools (<code>@tool</code>, skills, MCP) · point your custom logic at <code>agentic_rag/tools/</code></li>
        <li>register on <router-link to="/developer/agents">/developer/agents</router-link> · wait for admin approval · router auto-discovers</li>
      </ol>
    </TermBox>

    <!-- Section nav -->
    <TermBox title="contents" pad="sm">
      <ul class="toc">
        <li><a href="#what-you-fork">what you fork</a></li>
        <li><a href="#tools">add a tool · the @tool decorator</a></li>
        <li><a href="#middleware">middleware · trace / cost / guardrail / retry</a></li>
        <li><a href="#advanced">advanced · coordinator · bg task · skills · mcp</a></li>
        <li><a href="#endpoints">endpoints your agent must expose</a></li>
        <li><a href="#bootstrap">register · bootstrap · service token</a></li>
        <li><a href="#testing">testing &amp; quality gates</a></li>
        <li><a href="#troubleshoot">troubleshooting</a></li>
      </ul>
    </TermBox>

    <!-- What you fork -->
    <TermBox id="what-you-fork" title="what you fork" pad="md">
      <p class="lead">
        <strong>AgenticRAG</strong> is the official sub-agent template for Phase 1 of ANILA.
        Self-contained: zero ANILA-internal package dependencies.
        Third-party OSS (langchain, llama-index, sentence-transformers, …) is fine — just don't pull anila-* internal packages.
      </p>
      <p>
        The repo ships a vendored agent runtime at <code>agentic_rag/runtime/framework/</code>
        (47 modules covering Action / Agent / Runner / Middleware / StateMachine / Memory / Coordinator / BG&nbsp;Task / Skill / MCP),
        plus a complete RAG pipeline (vector_search / keyword_search / read_document, hierarchical chunker, vision-aware ingestion, cross-encoder reranker).
      </p>
      <p>
        Two endpoints coexist: <code>/chat</code> (legacy QueryEngine, 7-stage turn loop) and <code>/agentic-chat</code> (new framework Runner).
        Both emit the same SSE wire format. New forks should target <code>/agentic-chat</code>.
      </p>
    </TermBox>

    <!-- Tools -->
    <TermBox id="tools" title="add a tool · @tool decorator" pad="md">
      <p>
        The <code>@tool</code> decorator auto-generates JSON schema from Python type hints + Google-style docstring <code>Args:</code> block.
        Drop the function in <code>agentic_rag/tools/</code> (or any module) and wire it into your Agent's <code>actions=</code> tuple.
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
        the framework auto-converts return-dict to <code>ActionResult(output=dict)</code>; raise / return <code>ActionResult(error="...")</code> for failure.
        first param must be <code>ctx</code> / <code>context</code> / annotated <code>ActionContext</code>.
      </p>

      <h4>wire into an agent</h4>
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

      <h4>RAG tools (already shipped)</h4>
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
# Auto-registers vector_search / keyword_search / read_document.
# Each result is a Citation (chunk_id / document_title / heading_path / page / confidence).</pre>
    </TermBox>

    <!-- Middleware -->
    <TermBox id="middleware" title="middleware · 5 built-ins" pad="md">
      <p>
        Middleware composes around every Action call. Run-level middleware wraps action-level middleware wraps the handler.
        Order matters: first registered = outermost (sees input first, output last).
      </p>
      <table class="term-table">
        <thead>
          <tr><th>middleware</th><th>what it does</th><th>typical use</th></tr>
        </thead>
        <tbody>
          <tr>
            <td><code>TraceMiddleware</code></td>
            <td>open span around each action; record input/output/timing</td>
            <td>audit log; live tracing dashboard</td>
          </tr>
          <tr>
            <td><code>CostMiddleware</code></td>
            <td>token-count tracking; optional dollar tracking; budget gate</td>
            <td>per-agent capacity reports (ANILA = local, no $$ to track)</td>
          </tr>
          <tr>
            <td><code>GuardrailMiddleware</code></td>
            <td>input / output checks; allow / deny / modify decisions</td>
            <td>PII redaction; citation enforcement; prompt-injection guard</td>
          </tr>
          <tr>
            <td><code>RetryMiddleware</code></td>
            <td>exception or error-result retry with exponential backoff</td>
            <td>flaky reranker; vLLM hiccup recovery</td>
          </tr>
          <tr>
            <td><code>ShellHookMiddleware</code></td>
            <td>spawn shell command before/after action; JSON over stdin/stdout</td>
            <td>corporate audit pipe; ops-team-maintained deny-list</td>
          </tr>
          <tr>
            <td><code>ToolOutputTrimmerMiddleware</code></td>
            <td>cap tool output size; preview replacement when over threshold</td>
            <td>vector_search returns 50 chunks → don't blow context window</td>
          </tr>
        </tbody>
      </table>

      <h4>example · build a runner with trace + retry</h4>
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

      <h4>citation guardrail · enforce answers cite sources</h4>
      <pre class="code">from agentic_rag.runtime.bridge import enforce_citations

result = await runner.run(agent, "what does the docs say about X?")
verdict = enforce_citations(result, mode="warn")  # or mode="block" to raise
# verdict.cited / verdict.matched / verdict.candidates</pre>
    </TermBox>

    <!-- Advanced primitives -->
    <TermBox id="advanced" title="advanced primitives" pad="md">
      <h4>Coordinator · LLM-driven sub-agent fan-out</h4>
      <p>
        Spawn N parallel sub-agents (read-only) or sequential ones (write-safe).
        The coordinator <em>agent</em> is just an Agent whose <code>actions=</code> includes <code>spawn_worker</code> / <code>check_worker</code> / <code>wait_for_workers</code>.
      </p>
      <pre class="code">from agentic_rag.runtime.framework import Coordinator, make_coordinator_actions

coord = Coordinator(workers={"verifier": verifier_agent, "summariser": summ_agent})
coord_agent = Agent(
    name="orchestrator",
    instructions="Decompose the request. spawn_worker for each subtask. wait_for_workers. summarise.",
    provider=adapter, model="google/gemma4",
    actions=tuple(make_coordinator_actions(coord)),
)</pre>

      <h4>BG_TASK · long-running background work</h4>
      <p>
        For non-LLM batch jobs (ingest 10k PDFs, rebuild vector index, batch inference).
        Returns a handle immediately; the LLM uses <code>check_bg_task</code> / <code>cancel_bg_task</code> to control.
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
        Drop a <code>.md</code> file into <code>~/.agentic-rag/skills/</code>; non-coders can author tools.
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
# All skills:
agent = Agent(actions=tuple(registry.all_actions()), ...)
# Or filtered to query (saves prompt tokens):
relevant = registry.actions_for(user_query, limit=5)</pre>

      <h4>MCP · plug in third-party MCP servers</h4>
      <p>
        Connect to <code>mcp-server-filesystem</code>, <code>mcp-server-github</code>, <code>mcp-server-sentry</code>, or any custom stdio MCP server.
        Requires <code>pip install 'agentic-rag[mcp]'</code>.
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
    <TermBox id="endpoints" title="endpoints your agent must expose" pad="md">
      <p>The CSP router calls these. <code>api.py</code> in AgenticRAG already implements them.</p>
      <table class="term-table">
        <thead>
          <tr><th style="width: 70px">method</th><th>path</th><th>auth</th><th>purpose</th></tr>
        </thead>
        <tbody>
          <tr>
            <td><code>GET</code></td><td><code>/health</code></td><td>public</td>
            <td>discovery + health probe; returns <code>{"status":"ok"}</code></td>
          </tr>
          <tr>
            <td><code>GET</code></td><td><code>/v1/models</code></td><td>s2s</td>
            <td>list available model ids (OpenAI-compat)</td>
          </tr>
          <tr>
            <td><code>POST</code></td><td><code>/v1/chat/completions</code></td><td>s2s</td>
            <td>main inference (OpenAI-compat); SSE-stream by default</td>
          </tr>
          <tr>
            <td><code>POST</code></td><td><code>/agentic-chat</code></td><td>s2s</td>
            <td>framework Runner SSE stream (richer tool-call events)</td>
          </tr>
          <tr>
            <td><code>POST</code></td><td><code>/documents/upload</code></td><td>s2s</td>
            <td>RAG ingestion; multipart/form-data</td>
          </tr>
          <tr>
            <td><code>POST</code></td><td><code>/search</code></td><td>s2s</td>
            <td>raw retrieval (no LLM)</td>
          </tr>
        </tbody>
      </table>
      <p class="hint">
        s2s = service-to-service. Two auth headers run side-by-side: <code>X-CSP-Service-Token</code> from the platform, <code>Authorization: Bearer ...</code> for direct clients (OpenWebUI etc).
      </p>
    </TermBox>

    <!-- Bootstrap -->
    <TermBox id="bootstrap" title="register · bootstrap · service token" pad="md">
      <ol class="steps">
        <li>
          register your agent on <router-link to="/developer/agents">/developer/agents</router-link> ·
          status starts as <TermBadge variant="warn">pending</TermBadge>
        </li>
        <li>
          admin approves · status flips to <TermBadge variant="ok">approved</TermBadge>
        </li>
        <li>
          admin issues a one-shot <strong>bootstrap token</strong> (<code>bsk-...</code>, 15-min TTL)
        </li>
        <li>
          set in your agent's <code>.env</code>:
          <pre class="code">CSP_URL=http://csp:8000
ANILA_AGENT_ID=&lt;your-id&gt;
ANILA_ENDPOINT_URL=http://&lt;your-host&gt;:24786
CSP_BOOTSTRAP_TOKEN=bsk-XXXX-from-admin</pre>
        </li>
        <li>
          first <code>docker compose up -d</code> auto-runs the bootstrap CLI: trades the bsk for a long-lived <code>csk-...</code> service token written to <code>/var/lib/anila-agent/service_token.json</code> (mode 0600)
        </li>
        <li>
          remove <code>CSP_BOOTSTRAP_TOKEN</code> from <code>.env</code> · it's been consumed; CSP rejects replays
        </li>
        <li>
          router auto-discovers your <code>/health</code> · starts dispatching traffic
        </li>
      </ol>
      <p class="hint">
        full lifecycle (rotation, multi-replica K8s, fallback to fleet-shared <code>CSP_SERVICE_TOKEN</code>): see <code>AgenticRAG/docs/BOOTSTRAP_DEPLOYMENT.md</code>
      </p>
    </TermBox>

    <!-- Testing -->
    <TermBox id="testing" title="testing &amp; quality gates" pad="md">
      <pre class="code">pip install -e '.[rag,dev]'
pytest                                    # 632 tests
pytest --cov=agentic_rag --cov-report=term-missing
mypy src/agentic_rag/runtime/             # strict mode
ruff check src/ tests/</pre>
      <p>
        Quality bars maintained on the framework path: mypy strict + ruff clean + boundary test ensures no anila-* hard imports leak in.
        Add tests under <code>tests/</code> for your new tools — the <code>tests/runtime/framework/test_schema_generator.py</code> file is a good template for <code>@tool</code> coverage.
      </p>
    </TermBox>

    <!-- Troubleshooting -->
    <TermBox id="troubleshoot" title="troubleshooting" pad="md">
      <table class="term-table">
        <thead><tr><th>symptom</th><th>likely cause · fix</th></tr></thead>
        <tbody>
          <tr>
            <td><code>503</code> on every request</td>
            <td><code>API_KEY</code> not set and <code>API_DEV_MODE</code> not <code>true</code> · set one of them in <code>.env</code></td>
          </tr>
          <tr>
            <td>bootstrap CLI exits with <code>token consumed</code></td>
            <td>service-token state file already present · delete <code>/var/lib/anila-agent/service_token.json</code> if you want to re-bootstrap</td>
          </tr>
          <tr>
            <td>router never dispatches to your agent</td>
            <td>approval still <code>pending</code> · admin needs to act on <router-link to="/developer/agents">/developer/agents</router-link></td>
          </tr>
          <tr>
            <td>vector_search returns empty</td>
            <td>collection not assigned · ingestion not finished · embedding model mismatch (must match what was used at ingest)</td>
          </tr>
          <tr>
            <td>LLM keeps hallucinating tool names</td>
            <td>schema isn't reaching the model · check <code>agent.registry.tool_definitions()</code> output matches what your provider expects</td>
          </tr>
          <tr>
            <td>memory extraction storms after pod restart</td>
            <td>configure <code>CursorStore</code> on <code>MemoryExtractor</code> for persistence · see <code>agentic_rag/memory/extraction_state.py</code></td>
          </tr>
          <tr>
            <td>tool output blowing context window</td>
            <td>add <code>ToolOutputTrimmerMiddleware(max_chars=2000)</code> to runner middleware</td>
          </tr>
          <tr>
            <td>cancellation doesn't actually stop the run</td>
            <td>handler must check <code>ctx.metadata["_bg_cancel_signal"].is_set()</code> at await points; framework can't preempt arbitrary CPU loops</td>
          </tr>
        </tbody>
      </table>
    </TermBox>

    <!-- Footer cross-link -->
    <TermBox title="next steps" pad="md">
      <ul class="next">
        <li>register your agent → <router-link to="/developer/agents">/developer/agents</router-link></li>
        <li>browse knowledge collections → <router-link to="/knowledge-collections">/knowledge-collections</router-link></li>
        <li>deeper docs in <code>AgenticRAG/docs/</code> · <code>BOOTSTRAP_DEPLOYMENT.md</code> · <code>CSP_INTEGRATION.md</code></li>
        <li>v0.1 milestone changelog: see <code>AgenticRAG/README.md</code></li>
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
  margin: var(--gap-3) 0 var(--gap-2); text-transform: lowercase; letter-spacing: 0.02em;
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
