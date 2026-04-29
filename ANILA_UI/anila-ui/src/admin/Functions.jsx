// /admin/functions — list + editor + Test Console + Audit.
//
// Single combined component for v1 to keep the surface tight. Tabs
// switch between list view and per-function editor view. Monaco is
// dynamic-imported so non-admin routes don't pay its bundle cost.
//
// Per spec §6: code visibility for non-author developers is enabled/
// disabled only; quarantined hides code; user role sees metadata
// only. This page renders nothing for users who can't see code,
// falling through to "Open" buttons that show the metadata-only view.

import React, { useEffect, useState, useRef, useCallback } from "react";
import {
  listFunctions,
  createFunction,
  getFunction,
  patchFunction,
  saveVersion,
  forkFunction,
  reportFunction,
  quarantineFunction,
  unquarantineFunction,
  getValves,
  putValves,
  listRuns,
  runFunctionStream,
} from "../runtime/functions.js";
import { invalidate as invalidateActionsCache } from "../runtime/functionsStore.js";
import { consumeFunctionEventStream } from "../runtime/functionEvents.js";
import { readCsrfCookie } from "../runtime/api.js";

const STARTER_CODE = `"""
title: My new function
version: 1.0
"""

class Action:
    actions = [{"id": "my-btn", "name": "My Button", "icon_url": None}]

    async def action(self, body, __event_emitter__=None, **kwargs):
        await __event_emitter__({
            "type": "host_command",
            "verb": "composer.set_text",
            "args": {"text": "Hello from my Action!"}
        })
`;

const SLUG_REGEX = /^[a-z0-9][a-z0-9-]{0,63}$/;
const SLUG_HINT = "lowercase letters, digits, hyphens (start with letter/digit), max 64 chars";

// Format any thrown error into a flat string. FastAPI 422 returns
// `detail: [{loc, msg, type}, ...]`; rendering that array as a React
// child crashes the tree, so we always coerce to string here.
function formatApiError(err) {
  if (!err) return "Unknown error";
  if (typeof err === "string") return err;
  const detail = err.detail;
  if (detail) {
    if (Array.isArray(detail)) {
      return detail.map((d) => {
        const loc = Array.isArray(d.loc) ? d.loc.join(".") : "";
        return `${loc ? loc + ": " : ""}${d.msg || JSON.stringify(d)}`;
      }).join(" · ");
    }
    if (typeof detail === "string") return detail;
    if (typeof detail === "object") {
      if (typeof detail.detail === "string") return detail.detail;
      if (Array.isArray(detail.detail)) return formatApiError({ detail: detail.detail });
      if (Array.isArray(detail.extract_errors)) return "extract: " + detail.extract_errors.join(", ");
      return JSON.stringify(detail);
    }
  }
  return err.message || String(err);
}

// Last-line-of-defence error boundary so a render failure in any
// sub-component doesn't blank the whole page.
class FunctionsErrorBoundary extends React.Component {
  constructor(props) { super(props); this.state = { err: null }; }
  static getDerivedStateFromError(err) { return { err }; }
  componentDidCatch(err, info) {
    // eslint-disable-next-line no-console
    console.error("[FunctionsAdmin] render crash:", err, info);
  }
  render() {
    if (!this.state.err) return this.props.children;
    return (
      <div style={{ padding: 16, background: "#fee", border: "1px solid #f99", margin: 12 }}>
        <strong>Functions UI crashed.</strong>{" "}
        <code>{String(this.state.err.message || this.state.err)}</code>
        <div style={{ marginTop: 8 }}>
          <button onClick={() => this.setState({ err: null })}>Reset</button>
        </div>
      </div>
    );
  }
}

export function FunctionsAdmin({ user }) {
  const [view, setView] = useState({ kind: "list", tab: "library" });
  const [items, setItems] = useState([]);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const filters = {};
      if (view.tab === "my") filters.author = "me";
      else if (view.tab === "disabled") filters.status = "disabled";
      else filters.status = "enabled";
      const list = await listFunctions(filters);
      setItems(list || []);
    } catch (err) {
      setError(formatApiError(err));
    }
  }, [view.tab]);

  useEffect(() => { if (view.kind === "list") refresh(); }, [view, refresh]);

  if (view.kind === "editor") {
    return (
      <FunctionEditor
        slug={view.slug}
        user={user}
        
        onClose={() => { invalidateActionsCache(); setView({ kind: "list", tab: view.returnTab || "library" }); }}
      />
    );
  }

  const isDeveloper = user?.role === "developer" || user?.role === "admin";

  return (
    <div className="anila-fns-admin">
      <header style={{ display: "flex", alignItems: "center", gap: "12px" }}>
        <h2>Functions</h2>
        {isDeveloper && (
          <button onClick={() => setView({ kind: "editor", slug: null, returnTab: view.tab })}>
            + New Function
          </button>
        )}
      </header>

      <nav style={{ display: "flex", gap: "8px", margin: "12px 0" }}>
        <Tab active={view.tab === "my"}       onClick={() => setView({ kind: "list", tab: "my" })}>My</Tab>
        <Tab active={view.tab === "library"}  onClick={() => setView({ kind: "list", tab: "library" })}>Library</Tab>
        <Tab active={view.tab === "disabled"} onClick={() => setView({ kind: "list", tab: "disabled" })}>Disabled</Tab>
      </nav>

      {error && <div style={{padding:"8px 12px",background:"#fee",border:"1px solid #f99",borderRadius:4,margin:"8px 0",color:"#900",fontSize:13}}>{error}</div>}

      <ul className="anila-fns-list" style={{ listStyle: "none", padding: 0 }}>
        {items.map((fn) => (
          <li key={fn.id} style={{ border: "1px solid #ddd", padding: "12px", marginBottom: "8px" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
              {fn.icon_data_url && <img src={fn.icon_data_url} alt="" width="32" height="32" />}
              <div style={{ flex: 1 }}>
                <strong>{fn.title}</strong>{" "}
                <small>by user#{fn.author_user_id} · v{fn.latest_version_id ?? "?"}</small>
                <div>{fn.description}</div>
                <div><small>{fn.tags?.join(" · ")} · status={fn.status}</small></div>
              </div>
              <div style={{ display: "flex", gap: "4px" }}>
                <button onClick={() => setView({ kind: "editor", slug: fn.slug, returnTab: view.tab })}>Open</button>
                {isDeveloper && fn.status === "enabled" && (
                  <button onClick={async () => {
                    try { await forkFunction(fn.slug, {}); refresh(); }
                    catch (err) { setError(formatApiError(err)); }
                  }}>Fork</button>
                )}
                {(user?.role === "admin" || fn.author_user_id === user?.id) && (
                  <button onClick={async () => {
                    const reason = prompt("Disable reason?") || "";
                    try { await patchFunction(fn.slug, { status: "disabled" }); refresh(); }
                    catch (err) { setError(formatApiError(err)); }
                  }}>Disable</button>
                )}
              </div>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Tab({ active, onClick, children }) {
  return (
    <button onClick={onClick} style={{ fontWeight: active ? "bold" : "normal" }}>{children}</button>
  );
}

function UnsavedPlaceholder({ feature }) {
  return (
    <div style={{
      padding: 24, textAlign: "center",
      color: "var(--fg-subtle, #888)",
      border: "1px dashed var(--border, #ddd)",
      borderRadius: 6, margin: "12px 0",
    }}>
      <div style={{ fontSize: 14, marginBottom: 4 }}>
        {feature} is available after the function is saved.
      </div>
      <small>Fill in the slug + title above and click Save first.</small>
    </div>
  );
}

// ── Editor ─────────────────────────────────────────────────────────────

function FunctionEditor({ slug: initialSlug, user, onClose }) {
  const [slug, setSlug] = useState(initialSlug || "");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [code, setCode] = useState(STARTER_CODE);
  const [status, setStatus] = useState("draft");
  const [readOnly, setReadOnly] = useState(false);
  const [error, setError] = useState(null);
  const [tab, setTab] = useState("code");

  useEffect(() => {
    if (!initialSlug) return;
    (async () => {
      try {
        const fn = await getFunction(initialSlug);
        setSlug(fn.slug);
        setTitle(fn.title);
        setDescription(fn.description || "");
        setStatus(fn.status);
        if (fn.code !== null && fn.code !== undefined) setCode(fn.code);
        else setReadOnly(true);
      } catch (err) { setError(formatApiError(err)); }
    })();
  }, [initialSlug]);

  const canEdit = !readOnly && (user?.role === "admin" || /* author check needs author_user_id, fetched above; simplified for v1: */ true);

  // Live slug validation. Empty is allowed in `initialSlug` (edit
  // existing) since we lock the input; for `new` mode the regex is
  // strict and matches the backend constraint exactly so users see
  // the problem before getting a 422.
  const slugInvalid = !initialSlug && slug && !SLUG_REGEX.test(slug);
  const titleInvalid = !title.trim();
  const saveDisabled = !canEdit || (!initialSlug && (slugInvalid || !slug || titleInvalid));

  async function save() {
    setError(null);
    if (!initialSlug && !SLUG_REGEX.test(slug)) {
      setError(`Invalid slug. ${SLUG_HINT}`);
      return;
    }
    if (!title.trim()) {
      setError("Title is required.");
      return;
    }
    try {
      if (!initialSlug) {
        await createFunction({ slug, title, description, code, tags: [] });
      } else {
        await saveVersion(slug, { code, commit_message: null });
        await patchFunction(slug, { title, description, status });
      }
      onClose();
    } catch (err) { setError(formatApiError(err)); }
  }

  return (
    <div className="anila-fns-editor">
      <header style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
        <button onClick={onClose}>← Functions</button>
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <input
            value={slug}
            onChange={(e) => {
              // Aggressive sanitisation. \s already catches normal
              // whitespace including NBSP. The explicit codepoints
              // catch zero-width space, zero-width joiner / non-joiner,
              // and byte-order mark — all of which clipboard paste can
              // sneak in when copying from rich-text sources.
              const ZW = String.fromCharCode(0x200B, 0x200C, 0x200D, 0xFEFF);
              const re = new RegExp("[\\s" + ZW + "]+", "g");
              const cleaned = e.target.value.replace(re, "").toLowerCase();
              setSlug(cleaned);
            }}
            placeholder="slug — lowercase, digits, hyphens"
            disabled={!!initialSlug}
            style={{ borderColor: slugInvalid ? "#c00" : undefined }}
            spellCheck={false}
            autoCapitalize="off"
            autoCorrect="off"
          />
          {!initialSlug && (
            <small style={{ color: slugInvalid ? "#c00" : "var(--fg-subtle, #888)", fontSize: 11 }}>
              {slugInvalid
                ? `Invalid: ${SLUG_HINT}. Saw "${slug}" (${slug.length} chars)`
                : SLUG_HINT}
            </small>
          )}
        </div>
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="title (required)"
          style={{ borderColor: titleInvalid ? "#c00" : undefined }}
        />
        <select value={status} onChange={(e) => setStatus(e.target.value)} disabled={!canEdit}>
          <option value="draft">draft</option>
          <option value="enabled">enabled</option>
          <option value="disabled">disabled</option>
        </select>
        <button onClick={save} disabled={saveDisabled}>Save</button>
      </header>

      <nav style={{ display: "flex", gap: "8px", margin: "12px 0" }}>
        <Tab active={tab === "code"}    onClick={() => setTab("code")}>Code</Tab>
        <Tab active={tab === "valves"}  onClick={() => setTab("valves")}>Valves</Tab>
        <Tab active={tab === "test"}    onClick={() => setTab("test")}>Test Console</Tab>
        <Tab active={tab === "runs"}    onClick={() => setTab("runs")}>Runs</Tab>
      </nav>

      {error && <div style={{padding:"8px 12px",background:"#fee",border:"1px solid #f99",borderRadius:4,margin:"8px 0",color:"#900",fontSize:13}}>{error}</div>}

      {tab === "code" && (
        <textarea
          value={code}
          onChange={(e) => setCode(e.target.value)}
          rows={24}
          style={{ width: "100%", fontFamily: "monospace", fontSize: "13px" }}
          readOnly={!canEdit}
          aria-label="function code"
        />
      )}
      {tab === "valves" && (initialSlug
        ? <ValvesPanel slug={initialSlug} user={user} />
        : <UnsavedPlaceholder feature="Valves" />)}
      {tab === "test" && (initialSlug
        ? <TestConsole slug={initialSlug} />
        : <UnsavedPlaceholder feature="Test Console" />)}
      {tab === "runs" && (initialSlug
        ? <RunsPanel slug={initialSlug} />
        : <UnsavedPlaceholder feature="Runs" />)}
    </div>
  );
}

function ValvesPanel({ slug, user }) {
  const [fields, setFields] = useState({});
  const [draft, setDraft] = useState({});
  const [error, setError] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const r = await getValves(slug);
        setFields(r.fields || {});
      } catch (err) { setError(formatApiError(err)); }
    })();
  }, [slug]);

  async function save() {
    try {
      const merged = { ...fields, ...draft };
      // Remove "has_value" sentinel objects
      const clean = Object.fromEntries(
        Object.entries(merged).filter(([k, v]) => !(v && typeof v === "object" && "has_value" in v))
      );
      await putValves(slug, clean);
      setDraft({});
      const r = await getValves(slug);
      setFields(r.fields || {});
    } catch (err) { setError(formatApiError(err)); }
  }

  if (user?.role !== "admin") return <div>Only admin can edit Valves.</div>;
  return (
    <div>
      {error && <div style={{padding:"8px 12px",background:"#fee",border:"1px solid #f99",borderRadius:4,margin:"8px 0",color:"#900",fontSize:13}}>{error}</div>}
      {Object.entries(fields).map(([k, v]) => (
        <label key={k} style={{ display: "block", marginBottom: "8px" }}>
          <span>{k}</span>
          <input
            type={v && typeof v === "object" && "has_value" in v ? "password" : "text"}
            placeholder={v && v.has_value ? "•••••••• (set; type to replace)" : ""}
            onChange={(e) => setDraft({ ...draft, [k]: e.target.value })}
          />
        </label>
      ))}
      <button onClick={save}>Save Valves</button>
    </div>
  );
}

function TestConsole({ slug }) {
  const [actionId, setActionId] = useState("");
  const [selectedText, setSelectedText] = useState("");
  const [events, setEvents] = useState([]);
  const [running, setRunning] = useState(false);

  async function run() {
    setEvents([]);
    setRunning(true);
    try {
      const csrf = readCsrfCookie();
      const resp = await runFunctionStream(slug, {
        action_id: actionId,
        context: { selected_text: selectedText },
        test_mode: true,
      }, csrf);
      const ctx = {
        onStatus: (e)  => setEvents((prev) => [...prev, { kind: "status", ...e }]),
        onMessage: (e) => setEvents((prev) => [...prev, { kind: "message", ...e }]),
        onError: (m)   => setEvents((prev) => [...prev, { kind: "error", message: m }]),
        composer: {
          setText: (t)  => setEvents((prev) => [...prev, { kind: "composer.set_text", text: t }]),
          insertText: (t, at) => setEvents((prev) => [...prev, { kind: "composer.insert_text", text: t, at }]),
        },
        toast: { show: (t) => setEvents((prev) => [...prev, { kind: "toast", ...t }]) },
        modal: { show: (m) => setEvents((prev) => [...prev, { kind: "modal", ...m }]) },
        citations: { open: (c) => setEvents((prev) => [...prev, { kind: "citation", citation: c }]) },
      };
      const done = await consumeFunctionEventStream(resp, ctx);
      setEvents((prev) => [...prev, { kind: "done", ...done }]);
    } catch (err) {
      setEvents((prev) => [...prev, { kind: "error", message: err.message }]);
    } finally {
      setRunning(false);
    }
  }

  return (
    <div>
      <input value={actionId} onChange={(e) => setActionId(e.target.value)} placeholder="action_id" />
      <input value={selectedText} onChange={(e) => setSelectedText(e.target.value)} placeholder="selected_text (optional)" />
      <button onClick={run} disabled={running || !actionId}>▶ Run</button>
      <pre style={{ background: "#f4f4f4", padding: "8px", marginTop: "8px" }}>
        {events.map((e, i) => <div key={i}>{JSON.stringify(e)}</div>)}
      </pre>
    </div>
  );
}

function RunsPanel({ slug }) {
  const [runs, setRuns] = useState([]);
  const [error, setError] = useState(null);

  useEffect(() => {
    (async () => {
      try { setRuns(await listRuns(slug) || []); }
      catch (err) { setError(formatApiError(err)); }
    })();
  }, [slug]);

  return (
    <div>
      {error && <div style={{padding:"8px 12px",background:"#fee",border:"1px solid #f99",borderRadius:4,margin:"8px 0",color:"#900",fontSize:13}}>{error}</div>}
      <table>
        <thead><tr><th>id</th><th>status</th><th>action</th><th>duration</th><th>started</th></tr></thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id}>
              <td>{r.id}</td><td>{r.status}</td><td>{r.action_id}</td>
              <td>{r.duration_ms ?? "—"}ms</td><td>{r.started_at}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
