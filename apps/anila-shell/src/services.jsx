// 專案入口（Service Platform）— doc 00 §2 first-class 使用者入口 + doc 07 launch 契約。
//
// 從 GET /api/services 取得可存取的已註冊服務（7a 後端）；7a 尚未上線時
// 於 404 退回 legacy GET /api/platform-links。點選服務卡片後：
//   - new_tab：POST /api/services/{id}/launch → window.open(launch_url,'_blank','noopener')
//   - iframe：站內覆蓋層開啟 <iframe>（沙箱 + no-referrer），附「此服務由 <name> 提供」安全提示。
// legacy platform_links 無 launch 端點，直接以既有 url 於新分頁開啟。
//
// 尚未與任務（Task）耦合——Slice 9 才會接 task→service。

import React, { useCallback, useEffect, useState } from "react";

import { authRequest } from "./runtime/api.js";
import { IconExternal, IconGrid, IconShield, IconX } from "./icons.jsx";

// ---- 純資料層（供單元測試共用） --------------------------------------------

function unwrapList(data) {
  if (Array.isArray(data)) return data;
  if (Array.isArray(data?.services)) return data.services;
  if (Array.isArray(data?.data)) return data.data;
  if (Array.isArray(data?.items)) return data.items;
  return [];
}

// registered_services 是 platform_links 的超集；缺欄位時退回 legacy 值，
// 確保 7a 後端未上線也能運作（防禦式）。
export function normalizeServiceCard(row = {}, { legacy = false } = {}) {
  return {
    id: row.id,
    name: row.name || "未命名服務",
    description: row.description || "",
    icon: row.icon || "",
    url: row.url || row.entry_url || "",
    launchMode: row.launch_mode === "iframe" ? "iframe" : "new_tab",
    slug: row.slug || "",
    __legacy: legacy,
  };
}

// GET /api/services，404 → 退回 /api/platform-links。
export async function fetchServices(request = authRequest) {
  try {
    const data = await request("/api/services");
    return unwrapList(data).map((r) => normalizeServiceCard(r, { legacy: false }));
  } catch (err) {
    if (err?.status === 404) {
      const data = await request("/api/platform-links");
      return unwrapList(data).map((r) => normalizeServiceCard(r, { legacy: true }));
    }
    throw err;
  }
}

// 解析啟動：registry 服務走 POST launch 契約；legacy 直接用既有 url。
export async function resolveLaunch(request, service) {
  if (service.__legacy || service.id == null) {
    return { mode: service.launchMode || "new_tab", launch_url: service.url, launch_id: null };
  }
  const res = await request(`/api/services/${service.id}/launch`, {
    method: "POST",
    body: JSON.stringify({}),
  });
  return {
    mode: res?.mode || service.launchMode || "new_tab",
    launch_url: res?.launch_url || service.url,
    launch_id: res?.launch_id || null,
  };
}

// ---- UI 層 ------------------------------------------------------------------

function ServiceCard({ service, onLaunch }) {
  const glyph = (service.name || "?").slice(0, 1).toUpperCase();
  const modeLabel = service.launchMode === "iframe" ? "內嵌" : "新分頁";
  return (
    <button
      type="button"
      onClick={() => onLaunch(service)}
      style={{
        display: "flex", flexDirection: "column", gap: 8, textAlign: "left",
        padding: 14,
        background: "var(--bg-elev)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius-lg)",
        cursor: "pointer",
        transition: "border-color .12s, transform .12s",
        color: "var(--fg)",
      }}
      onMouseEnter={(e) => { e.currentTarget.style.borderColor = "var(--border-strong)"; e.currentTarget.style.transform = "translateY(-1px)"; }}
      onMouseLeave={(e) => { e.currentTarget.style.borderColor = "var(--border)"; e.currentTarget.style.transform = ""; }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <div style={{
          width: 34, height: 34, borderRadius: "var(--radius)",
          background: "var(--bg-subtle)", border: "1px solid var(--border)",
          display: "inline-flex", alignItems: "center", justifyContent: "center",
          fontWeight: 600, fontFamily: "var(--font-mono)", color: "var(--fg-muted)",
        }}>{glyph}</div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{service.name}</div>
        </div>
        {/* pill：啟動模式 */}
        <span style={{
          display: "inline-flex", alignItems: "center", gap: 4,
          padding: "2px 8px", fontSize: 11, fontFamily: "var(--font-mono)",
          background: "var(--bg-subtle)", border: "1px solid var(--border)",
          borderRadius: 999, color: "var(--fg-muted)",
        }}>
          {service.launchMode === "iframe" ? <IconGrid size={11} /> : <IconExternal size={11} />}
          {modeLabel}
        </span>
      </div>
      <div style={{ fontSize: 12, color: "var(--fg-muted)", lineHeight: 1.5, minHeight: 18 }}>
        {service.description || "無描述"}
      </div>
    </button>
  );
}

function IframeOverlay({ url, name, onClose }) {
  return (
    <div style={{
      position: "fixed", inset: 0, zIndex: 120,
      display: "flex", flexDirection: "column",
      background: "var(--bg)",
    }}>
      {/* 安全提示橫幅 */}
      <div role="note" style={{
        display: "flex", alignItems: "center", gap: 10,
        padding: "10px 16px",
        background: "var(--bg-subtle)",
        borderBottom: "1px solid var(--border)",
        fontSize: 12,
      }}>
        <IconShield size={14} style={{ color: "var(--fg-muted)", flexShrink: 0 }} />
        <span style={{ flex: 1, minWidth: 0 }}>
          此服務由 <b>{name}</b> 提供 · 內容於受限沙箱中執行，請勿於此輸入院外機敏資料。
        </span>
        <button
          type="button"
          onClick={onClose}
          aria-label="關閉服務"
          style={{
            display: "inline-flex", alignItems: "center", gap: 6,
            padding: "5px 12px", fontSize: 12, fontWeight: 500,
            background: "var(--bg-elev)", border: "1px solid var(--border)",
            borderRadius: "var(--radius)", color: "var(--fg)", cursor: "pointer",
          }}
        >
          <IconX size={13} /> 關閉
        </button>
      </div>
      <iframe
        title={name}
        src={url}
        sandbox="allow-scripts allow-same-origin allow-forms"
        referrerPolicy="no-referrer"
        style={{ flex: 1, width: "100%", border: 0, background: "var(--bg)" }}
      />
    </div>
  );
}

export function ServicesPanel({ open, onClose, request = authRequest, toast }) {
  const [state, setState] = useState({ loading: false, error: "", services: [] });
  const [iframe, setIframe] = useState(null); // { url, name } | null
  const [launchError, setLaunchError] = useState("");

  const load = useCallback(async () => {
    setState((s) => ({ ...s, loading: true, error: "" }));
    try {
      const services = await fetchServices(request);
      setState({ loading: false, error: "", services });
    } catch (err) {
      setState({ loading: false, error: err?.message || "載入服務清單失敗", services: [] });
    }
  }, [request]);

  useEffect(() => {
    if (!open) return;
    setLaunchError("");
    setIframe(null);
    void load();
  }, [open, load]);

  if (!open) return null;

  async function handleLaunch(service) {
    setLaunchError("");
    try {
      const { mode, launch_url } = await resolveLaunch(request, service);
      if (!launch_url) throw new Error("服務未提供啟動網址");
      if (mode === "iframe") {
        setIframe({ url: launch_url, name: service.name });
      } else {
        window.open(launch_url, "_blank", "noopener");
      }
    } catch (err) {
      const msg = err?.message || `無法啟動「${service.name}」`;
      setLaunchError(msg);
      toast?.(msg, { tone: "error" });
    }
  }

  if (iframe) {
    return <IframeOverlay url={iframe.url} name={iframe.name} onClose={() => setIframe(null)} />;
  }

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, zIndex: 110,
        background: "oklch(0.10 0 0 / 0.4)",
        display: "flex", alignItems: "stretch", justifyContent: "center",
        padding: 24,
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="專案入口"
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--bg-elev)",
          border: "1px solid var(--border)",
          borderRadius: "var(--radius-lg)",
          width: "100%", maxWidth: 860, maxHeight: "100%",
          display: "flex", flexDirection: "column",
          boxShadow: "0 24px 60px -20px oklch(0.10 0 0 / 0.35)",
          overflow: "hidden",
        }}
      >
        <div style={{
          display: "flex", alignItems: "center", gap: 10,
          padding: "16px 20px 12px",
          borderBottom: "1px solid var(--border)",
        }}>
          <IconGrid size={16} />
          <div>
            <div style={{ fontSize: 15, fontWeight: 600 }}>專案入口</div>
            <div style={{ fontSize: 12, color: "var(--fg-muted)", marginTop: 2 }}>
              由平台治理中心註冊、依你的權限開放的服務
            </div>
          </div>
          <div style={{ flex: 1 }} />
          <button
            type="button"
            onClick={onClose}
            aria-label="關閉"
            style={{
              display: "inline-flex", alignItems: "center", justifyContent: "center",
              width: 30, height: 30, background: "transparent",
              border: "1px solid transparent", borderRadius: "var(--radius)",
              color: "var(--fg-muted)", cursor: "pointer",
            }}
          ><IconX /></button>
        </div>

        <div style={{ padding: 20, overflowY: "auto" }}>
          {launchError && (
            <div role="alert" style={{
              marginBottom: 14, padding: "8px 12px",
              background: "oklch(0.97 0.03 25)", border: "1px solid var(--danger)",
              borderRadius: "var(--radius)", color: "var(--danger)", fontSize: 12,
            }}>{launchError}</div>
          )}

          {state.loading ? (
            <div style={{ padding: "40px 0", textAlign: "center", color: "var(--fg-muted)", fontSize: 13 }}>
              服務清單載入中…
            </div>
          ) : state.error ? (
            <div role="alert" style={{ padding: "40px 0", textAlign: "center", color: "var(--danger)", fontSize: 13 }}>
              {state.error}
            </div>
          ) : state.services.length === 0 ? (
            <div style={{ padding: "40px 0", textAlign: "center", color: "var(--fg-muted)", fontSize: 13 }}>
              目前沒有可用的服務入口。
            </div>
          ) : (
            <div style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))",
              gap: 12,
            }}>
              {state.services.map((s) => (
                <ServiceCard key={s.id ?? s.slug ?? s.name} service={s} onLaunch={handleLaunch} />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
