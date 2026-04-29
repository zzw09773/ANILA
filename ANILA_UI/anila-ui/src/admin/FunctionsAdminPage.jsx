// Top-level page wrapper for ANILA Functions v1 admin UI.
//
// Pulls user identity from AuthContext, renders a small header with
// a "← Back to chat" link plus role-aware title, then mounts the
// :py:class:`FunctionsAdmin` panel which owns list + editor + Test
// Console + Audit.

import React from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../runtime/auth.jsx";
import { FunctionsAdmin } from "./Functions.jsx";

class PageErrorBoundary extends React.Component {
  constructor(props) { super(props); this.state = { err: null }; }
  static getDerivedStateFromError(err) { return { err }; }
  componentDidCatch(err, info) {
    // eslint-disable-next-line no-console
    console.error("[FunctionsAdminPage] crash:", err, info);
  }
  render() {
    if (!this.state.err) return this.props.children;
    return (
      <div style={{ padding: 24, background: "#fee", border: "1px solid #f99", margin: 12, borderRadius: 8 }}>
        <h2 style={{ marginTop: 0 }}>Functions admin crashed</h2>
        <p style={{ fontFamily: "monospace", fontSize: 13 }}>
          {String(this.state.err.message || this.state.err)}
        </p>
        <p style={{ fontSize: 12, color: "#666" }}>
          Open the browser console for the full stack. Then click Reset to try again.
        </p>
        <button onClick={() => this.setState({ err: null })}>Reset</button>
        <Link to="/app" style={{ marginLeft: 12 }}>← Back to chat</Link>
      </div>
    );
  }
}

export function FunctionsAdminPage() {
  const { user } = useAuth();
  return (
    <div style={{ minHeight: "100vh", background: "var(--bg)", padding: "16px 24px" }}>
      <header style={{
        display: "flex", alignItems: "center", gap: 16, marginBottom: 24,
        paddingBottom: 12, borderBottom: "1px solid var(--border, #e5e7eb)",
      }}>
        <Link to="/app" style={{ textDecoration: "none", color: "var(--fg-muted, #666)" }}>
          ← Back to chat
        </Link>
        <h1 style={{ margin: 0, fontSize: 18, flex: 1 }}>ANILA Functions</h1>
        <span style={{ fontSize: 12, color: "var(--fg-subtle, #999)" }}>
          {user?.username} · {user?.role || "user"}
        </span>
      </header>
      <PageErrorBoundary>
        <FunctionsAdmin user={user} />
      </PageErrorBoundary>
    </div>
  );
}
