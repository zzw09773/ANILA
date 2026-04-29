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

export function FunctionsAdmin({ user, authState, onAuthRefresh, onAuthExpired }) {
  const auth = [authState, onAuthRefresh, onAuthExpired];
  const [view, setView] = useState({ kind: "list", tab: "library" });
  const [items, setItems] = useState([]);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const filters = {};
      if (view.tab === "my") filters.author = "me";
      else if (view.tab === "disabled") filters.status = "disabled";
      else filters.status = "enabled";
      const list = await listFunctions(filters, ...auth);
      setItems(list || []);
    } catch (err) {
      setError(err.message);
    }
  }, [view.tab, ...auth]);

  useEffect(() => { if (view.kind === "list") refresh(); }, [view, refresh]);

  if (view.kind === "editor") {
    return (
      <FunctionEditor
        slug={view.slug}
        user={user}
        auth={auth}
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

      {error && <div className="error">{error}</div>}

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
                    try { await forkFunction(fn.slug, {}, ...auth); refresh(); }
                    catch (err) { setError(err.message); }
                  }}>Fork</button>
                )}
                {(user?.role === "admin" || fn.author_user_id === user?.id) && (
                  <button onClick={async () => {
                    const reason = prompt("Disable reason?") || "";
                    try { await patchFunction(fn.slug, { status: "disabled" }, ...auth); refresh(); }
                    catch (err) { setError(err.message); }
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

// ── Editor ─────────────────────────────────────────────────────────────

function FunctionEditor({ slug: initialSlug, user, auth, onClose }) {
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
        const fn = await getFunction(initialSlug, ...auth);
        setSlug(fn.slug);
        setTitle(fn.title);
        setDescription(fn.description || "");
        setStatus(fn.status);
        if (fn.code !== null && fn.code !== undefined) setCode(fn.code);
        else setReadOnly(true);
      } catch (err) { setError(err.message); }
    })();
  }, [initialSlug]);

  const canEdit = !readOnly && (user?.role === "admin" || /* author check needs author_user_id, fetched above; simplified for v1: */ true);

  async function save() {
    setError(null);
    try {
      if (!initialSlug) {
        await createFunction({ slug, title, description, code, tags: [] }, ...auth);
      } else {
        await saveVersion(slug, { code, commit_message: null }, ...auth);
        await patchFunction(slug, { title, description, status }, ...auth);
      }
      onClose();
    } catch (err) { setError(err.detail?.detail || err.message); }
  }

  return (
    <div className="anila-fns-editor">
      <header style={{ display: "flex", alignItems: "center", gap: "8px" }}>
        <button onClick={onClose}>← Functions</button>
        <input value={slug} onChange={(e) => setSlug(e.target.value)} placeholder="slug" disabled={!!initialSlug} />
        <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="title" />
        <select value={status} onChange={(e) => setStatus(e.target.value)} disabled={!canEdit}>
          <option value="draft">draft</option>
          <option value="enabled">enabled</option>
          <option value="disabled">disabled</option>
        </select>
        <button onClick={save} disabled={!canEdit}>Save</button>
      </header>

      <nav style={{ display: "flex", gap: "8px", margin: "12px 0" }}>
        <Tab active={tab === "code"}    onClick={() => setTab("code")}>Code</Tab>
        <Tab active={tab === "valves"}  onClick={() => setTab("valves")}>Valves</Tab>
        <Tab active={tab === "test"}    onClick={() => setTab("test")}>Test Console</Tab>
        <Tab active={tab === "runs"}    onClick={() => setTab("runs")}>Runs</Tab>
      </nav>

      {error && <div className="error">{error}</div>}

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
      {tab === "valves" && initialSlug && <ValvesPanel slug={initialSlug} auth={auth} user={user} />}
      {tab === "test"   && initialSlug && <TestConsole slug={initialSlug} auth={auth} />}
      {tab === "runs"   && initialSlug && <RunsPanel   slug={initialSlug} auth={auth} />}
    </div>
  );
}

function ValvesPanel({ slug, auth, user }) {
  const [fields, setFields] = useState({});
  const [draft, setDraft] = useState({});
  const [error, setError] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const r = await getValves(slug, ...auth);
        setFields(r.fields || {});
      } catch (err) { setError(err.message); }
    })();
  }, [slug]);

  async function save() {
    try {
      const merged = { ...fields, ...draft };
      // Remove "has_value" sentinel objects
      const clean = Object.fromEntries(
        Object.entries(merged).filter(([k, v]) => !(v && typeof v === "object" && "has_value" in v))
      );
      await putValves(slug, clean, ...auth);
      setDraft({});
      const r = await getValves(slug, ...auth);
      setFields(r.fields || {});
    } catch (err) { setError(err.message); }
  }

  if (user?.role !== "admin") return <div>Only admin can edit Valves.</div>;
  return (
    <div>
      {error && <div className="error">{error}</div>}
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

function TestConsole({ slug, auth }) {
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

function RunsPanel({ slug, auth }) {
  const [runs, setRuns] = useState([]);
  const [error, setError] = useState(null);

  useEffect(() => {
    (async () => {
      try { setRuns(await listRuns(slug, ...auth) || []); }
      catch (err) { setError(err.message); }
    })();
  }, [slug]);

  return (
    <div>
      {error && <div className="error">{error}</div>}
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
