// Admin: Service Access Management
//
// One page covering everything multi-service-integration-plan §7.5.3 spells
// out: list every platform_link, show its current active grants, let admin
// open a modal to grant a new user / department, or revoke an existing one.
// Admin-only — non-admin sessions are bounced back to /app.
//
// Backend contract (4 endpoints, all already shipped — see
// myCSPPlatform/backend/app/api/service_access_grants.py):
//   GET    /api/platform-links?include_inactive=true   (admin sees everything)
//   GET    /api/service-access-grants                  (active by default)
//   POST   /api/service-access-grants                  ({user_id|dept_id, link_id})
//   DELETE /api/service-access-grants/{grant_id}        (idempotent soft-revoke)
// Plus /api/users + /api/departments to populate the modal selects.

import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Link, Navigate } from "react-router-dom";

import { Button, Input, Modal } from "../components.jsx";
import { useAuth } from "../runtime/auth.jsx";

const ACCENT = "var(--accent)";
const FG = "var(--fg)";
const FG_MUTED = "var(--fg-muted)";
const FG_SUBTLE = "var(--fg-subtle)";
const BG_ELEV = "var(--bg-elev)";
const BG_SUBTLE = "var(--bg-subtle)";
const BORDER = "var(--border)";
const DANGER = "var(--danger)";
const RADIUS = "var(--radius)";

function Badge({ children, tone = "neutral" }) {
  const tones = {
    neutral: { background: BG_SUBTLE, color: FG_MUTED, border: BORDER },
    accent: { background: "var(--accent-soft)", color: ACCENT, border: ACCENT },
    danger: { background: "var(--bg-subtle)", color: DANGER, border: DANGER },
  };
  const palette = tones[tone] || tones.neutral;
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        padding: "2px 8px",
        fontSize: 11,
        fontFamily: "var(--font-mono)",
        background: palette.background,
        color: palette.color,
        border: `1px solid ${palette.border}`,
        borderRadius: 999,
        lineHeight: 1.4,
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </span>
  );
}

function PageHeader() {
  return (
    <header
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        padding: "20px 28px",
        borderBottom: `1px solid ${BORDER}`,
        background: BG_ELEV,
      }}
    >
      <div>
        <h1 style={{ margin: 0, fontSize: 18, fontWeight: 600 }}>
          服務存取權限管理
        </h1>
        <p style={{ margin: "4px 0 0", fontSize: 12, color: FG_MUTED }}>
          管理 platform_links 的個別 user / 部門 grant —
          <code style={{ marginLeft: 4, fontFamily: "var(--font-mono)" }}>
            §7.5.3
          </code>
        </p>
      </div>
      <Link to="/app" style={{ textDecoration: "none" }}>
        <Button variant="ghost">← 回 ANILA</Button>
      </Link>
    </header>
  );
}

function GrantRow({ grant, link, target, granter, onRevoke }) {
  const dateLabel = grant.granted_at
    ? new Date(grant.granted_at).toLocaleString("zh-TW", {
        dateStyle: "short",
        timeStyle: "short",
      })
    : "—";

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "1fr auto",
        alignItems: "center",
        gap: 12,
        padding: "10px 14px",
        borderTop: `1px solid ${BORDER}`,
        fontSize: 13,
      }}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Badge tone="accent">{grant.user_id != null ? "USER" : "DEPT"}</Badge>
          <span style={{ fontWeight: 500, color: FG }}>{target}</span>
        </div>
        <div style={{ fontSize: 11, color: FG_SUBTLE }}>
          {dateLabel} · 授權者 {granter}
        </div>
      </div>
      <Button variant="danger" size="sm" onClick={() => onRevoke(grant)}>
        Revoke
      </Button>
    </div>
  );
}

function NewGrantModal({ open, onClose, link, users, departments, onSubmit }) {
  const [tab, setTab] = useState("user");
  const [selectedId, setSelectedId] = useState("");
  const [filter, setFilter] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [errMsg, setErrMsg] = useState("");

  useEffect(() => {
    if (!open) return;
    setTab("user");
    setSelectedId("");
    setFilter("");
    setErrMsg("");
    setSubmitting(false);
  }, [open]);

  const filteredUsers = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return users;
    return users.filter(
      (u) =>
        u.username.toLowerCase().includes(q) ||
        (u.email || "").toLowerCase().includes(q),
    );
  }, [users, filter]);

  const filteredDepts = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return departments;
    return departments.filter((d) => d.name.toLowerCase().includes(q));
  }, [departments, filter]);

  async function handleSubmit() {
    if (!selectedId) {
      setErrMsg("請選擇授權對象");
      return;
    }
    const target =
      tab === "user"
        ? { user_id: Number(selectedId) }
        : { department_id: Number(selectedId) };
    setSubmitting(true);
    try {
      await onSubmit(target);
    } catch (err) {
      setErrMsg(err.message || "授權失敗");
      setSubmitting(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`授權 ${link?.name || ""}`}
      subtitle="選一個 user 或部門 — 同一目標只能有一筆 active grant"
      width={520}
    >
      <div style={{ display: "flex", gap: 8, marginBottom: 14 }}>
        <Button
          variant={tab === "user" ? "primary" : "subtle"}
          size="sm"
          onClick={() => {
            setTab("user");
            setSelectedId("");
          }}
        >
          User-level
        </Button>
        <Button
          variant={tab === "department" ? "primary" : "subtle"}
          size="sm"
          onClick={() => {
            setTab("department");
            setSelectedId("");
          }}
        >
          Department-level
        </Button>
      </div>

      <Input
        placeholder={tab === "user" ? "搜尋 username / email" : "搜尋部門名稱"}
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />

      <div
        style={{
          marginTop: 12,
          maxHeight: 280,
          overflowY: "auto",
          border: `1px solid ${BORDER}`,
          borderRadius: RADIUS,
          background: BG_SUBTLE,
        }}
      >
        {tab === "user"
          ? filteredUsers.map((u) => (
              <button
                key={u.id}
                type="button"
                onClick={() => setSelectedId(String(u.id))}
                style={{
                  display: "block",
                  width: "100%",
                  textAlign: "left",
                  padding: "10px 14px",
                  background:
                    String(u.id) === selectedId ? "var(--accent-soft)" : "transparent",
                  border: "none",
                  borderBottom: `1px solid ${BORDER}`,
                  cursor: "pointer",
                  fontSize: 13,
                  color: FG,
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
                  <span style={{ fontWeight: 500 }}>{u.username}</span>
                  <span style={{ fontSize: 11, color: FG_SUBTLE }}>
                    {u.role}
                    {u.department_name ? ` · ${u.department_name}` : ""}
                  </span>
                </div>
                {u.email ? (
                  <div style={{ fontSize: 11, color: FG_MUTED, marginTop: 2 }}>
                    {u.email}
                  </div>
                ) : null}
              </button>
            ))
          : filteredDepts.length === 0
            ? (
              <div style={{ padding: 16, fontSize: 12, color: FG_SUBTLE }}>
                目前沒有部門。請先到 部門管理 建立。
              </div>
            )
            : filteredDepts.map((d) => (
                <button
                  key={d.id}
                  type="button"
                  onClick={() => setSelectedId(String(d.id))}
                  style={{
                    display: "block",
                    width: "100%",
                    textAlign: "left",
                    padding: "10px 14px",
                    background:
                      String(d.id) === selectedId
                        ? "var(--accent-soft)"
                        : "transparent",
                    border: "none",
                    borderBottom: `1px solid ${BORDER}`,
                    cursor: "pointer",
                    fontSize: 13,
                    color: FG,
                  }}
                >
                  <div style={{ display: "flex", justifyContent: "space-between" }}>
                    <span style={{ fontWeight: 500 }}>{d.name}</span>
                    <span style={{ fontSize: 11, color: FG_SUBTLE }}>
                      {d.user_count ?? 0} 位使用者
                    </span>
                  </div>
                </button>
              ))}
      </div>

      {errMsg ? (
        <div style={{ marginTop: 10, fontSize: 12, color: DANGER }}>{errMsg}</div>
      ) : null}

      <div
        style={{
          display: "flex",
          justifyContent: "flex-end",
          gap: 8,
          marginTop: 16,
        }}
      >
        <Button variant="ghost" onClick={onClose}>
          取消
        </Button>
        <Button
          variant="primary"
          onClick={handleSubmit}
          disabled={submitting}
        >
          {submitting ? "授權中…" : "授權"}
        </Button>
      </div>
    </Modal>
  );
}

function LinkCard({
  link,
  grants,
  expanded,
  onToggle,
  onAddUserGrant,
  onAddDeptGrant,
  onRevoke,
  userMap,
  deptMap,
  granterMap,
}) {
  const activeCount = grants.filter((g) => !g.revoked_at).length;
  const required = link.required_roles || [];

  return (
    <div
      style={{
        background: BG_ELEV,
        border: `1px solid ${BORDER}`,
        borderRadius: "var(--radius-lg)",
        overflow: "hidden",
      }}
    >
      <button
        type="button"
        onClick={onToggle}
        style={{
          display: "grid",
          gridTemplateColumns: "1fr auto",
          gap: 12,
          alignItems: "center",
          width: "100%",
          padding: "14px 18px",
          background: "transparent",
          border: "none",
          textAlign: "left",
          cursor: "pointer",
        }}
      >
        <div style={{ minWidth: 0 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              flexWrap: "wrap",
            }}
          >
            <span style={{ fontSize: 14, fontWeight: 600 }}>{link.name}</span>
            {link.is_active ? null : <Badge tone="danger">已停用</Badge>}
            {link.is_public ? <Badge tone="accent">public</Badge> : null}
            {required.length > 0 ? (
              <Badge>{required.join(" / ")}</Badge>
            ) : (
              <Badge>role gate: 開放</Badge>
            )}
          </div>
          <div
            style={{
              fontSize: 11,
              color: FG_SUBTLE,
              marginTop: 4,
              fontFamily: "var(--font-mono)",
              wordBreak: "break-all",
            }}
          >
            {link.url}
          </div>
        </div>
        <div style={{ textAlign: "right" }}>
          <div style={{ fontSize: 11, color: FG_MUTED }}>active grants</div>
          <div style={{ fontSize: 18, fontWeight: 600, color: FG }}>{activeCount}</div>
        </div>
      </button>

      {expanded ? (
        <div style={{ borderTop: `1px solid ${BORDER}`, background: BG_SUBTLE }}>
          <div
            style={{
              display: "flex",
              gap: 8,
              padding: "10px 14px",
              borderBottom: `1px solid ${BORDER}`,
            }}
          >
            <Button variant="primary" size="sm" onClick={onAddUserGrant}>
              + 給 user
            </Button>
            <Button variant="default" size="sm" onClick={onAddDeptGrant}>
              + 給部門
            </Button>
            <span
              style={{
                marginLeft: "auto",
                fontSize: 11,
                color: FG_SUBTLE,
                alignSelf: "center",
              }}
            >
              {grants.length === 0 ? "尚未發出任何 grant" : `${grants.length} 筆`}
            </span>
          </div>

          {grants.length === 0 ? (
            <div style={{ padding: "16px", fontSize: 12, color: FG_SUBTLE }}>
              這個 link 還沒有 active grant。
              {link.is_public ? "（已 public，user 通過 role gate 即可看到。）" : ""}
            </div>
          ) : (
            grants.map((g) => {
              const target =
                g.user_id != null
                  ? userMap.get(g.user_id) || `user#${g.user_id}`
                  : deptMap.get(g.department_id) || `dept#${g.department_id}`;
              const granter = g.granted_by
                ? granterMap.get(g.granted_by) || `user#${g.granted_by}`
                : "system";
              return (
                <GrantRow
                  key={g.id}
                  grant={g}
                  link={link}
                  target={target}
                  granter={granter}
                  onRevoke={onRevoke}
                />
              );
            })
          )}
        </div>
      ) : null}
    </div>
  );
}

export default function AdminAccessPage() {
  const { authReady, user, authRequest } = useAuth();

  const [links, setLinks] = useState([]);
  const [grants, setGrants] = useState([]);
  const [users, setUsers] = useState([]);
  const [departments, setDepartments] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [expandedId, setExpandedId] = useState(null);
  const [grantModal, setGrantModal] = useState(null);

  const loadAll = useCallback(async () => {
    setLoading(true);
    try {
      const [linksRes, grantsRes, usersRes, deptsRes] = await Promise.all([
        authRequest("/api/platform-links?include_inactive=true"),
        authRequest("/api/service-access-grants"),
        authRequest("/api/users"),
        authRequest("/api/departments"),
      ]);
      setLinks(linksRes || []);
      setGrants(grantsRes || []);
      setUsers(usersRes || []);
      setDepartments(deptsRes || []);
      setError(null);
    } catch (err) {
      setError(err.message || "載入失敗");
    } finally {
      setLoading(false);
    }
  }, [authRequest]);

  useEffect(() => {
    if (!authReady) return;
    if (user?.role !== "admin") return;
    loadAll();
  }, [authReady, user, loadAll]);

  const grantsByLink = useMemo(() => {
    const out = new Map();
    for (const g of grants) {
      if (!out.has(g.platform_link_id)) out.set(g.platform_link_id, []);
      out.get(g.platform_link_id).push(g);
    }
    return out;
  }, [grants]);

  const userMap = useMemo(
    () => new Map(users.map((u) => [u.id, u.username])),
    [users],
  );
  const deptMap = useMemo(
    () => new Map(departments.map((d) => [d.id, d.name])),
    [departments],
  );
  const granterMap = userMap;

  if (!authReady) return null;
  if (user?.role !== "admin") return <Navigate to="/app" replace />;

  async function handleCreateGrant(target) {
    const linkId = grantModal?.linkId;
    if (linkId == null) return;
    await authRequest("/api/service-access-grants", {
      method: "POST",
      body: JSON.stringify({ platform_link_id: linkId, ...target }),
    });
    setGrantModal(null);
    await loadAll();
  }

  async function handleRevoke(grant) {
    const target =
      grant.user_id != null
        ? userMap.get(grant.user_id) || `user#${grant.user_id}`
        : deptMap.get(grant.department_id) || `dept#${grant.department_id}`;
    if (!window.confirm(`確認 revoke ${target} 的 grant？此動作可由再次 grant 還原。`)) {
      return;
    }
    try {
      await authRequest(`/api/service-access-grants/${grant.id}`, {
        method: "DELETE",
      });
      await loadAll();
    } catch (err) {
      window.alert(err.message || "Revoke 失敗");
    }
  }

  return (
    <div style={{ minHeight: "100vh", background: "var(--bg)" }}>
      <PageHeader />

      <main
        style={{
          maxWidth: 960,
          margin: "0 auto",
          padding: "28px",
          display: "flex",
          flexDirection: "column",
          gap: 14,
        }}
      >
        {error ? (
          <div
            style={{
              padding: "12px 14px",
              border: `1px solid ${DANGER}`,
              borderRadius: RADIUS,
              fontSize: 13,
              color: DANGER,
              background: BG_ELEV,
            }}
          >
            {error}
          </div>
        ) : null}

        {loading ? (
          <div style={{ fontSize: 13, color: FG_MUTED }}>載入中…</div>
        ) : (
          links
            .slice()
            .sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0))
            .map((link) => (
              <LinkCard
                key={link.id}
                link={link}
                grants={grantsByLink.get(link.id) || []}
                expanded={expandedId === link.id}
                onToggle={() =>
                  setExpandedId(expandedId === link.id ? null : link.id)
                }
                onAddUserGrant={() =>
                  setGrantModal({ linkId: link.id, type: "user" })
                }
                onAddDeptGrant={() =>
                  setGrantModal({ linkId: link.id, type: "department" })
                }
                onRevoke={handleRevoke}
                userMap={userMap}
                deptMap={deptMap}
                granterMap={granterMap}
              />
            ))
        )}
      </main>

      <NewGrantModal
        open={grantModal != null}
        link={links.find((l) => l.id === grantModal?.linkId)}
        users={users}
        departments={departments}
        onClose={() => setGrantModal(null)}
        onSubmit={handleCreateGrant}
      />
    </div>
  );
}
