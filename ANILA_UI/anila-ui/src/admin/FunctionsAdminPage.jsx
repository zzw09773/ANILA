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
      <FunctionsAdmin user={user} />
    </div>
  );
}
