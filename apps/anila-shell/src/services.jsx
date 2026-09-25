// 專案入口（Service Platform）— doc 00 §2 first-class 使用者入口 + doc 07 launch 契約。
//
// 從 GET /api/services 取得可存取的已註冊服務（7a 後端）；7a 尚未上線時
// 於 404 退回 legacy GET /api/platform-links。點選服務卡片後：
//   - new_tab：POST /api/services/{id}/launch → window.open(launch_url,'_blank','noopener')
//   - iframe：**跨 origin** 服務才用站內覆蓋層開啟 <iframe>（沙箱 + no-referrer），
//     附「此服務由 <name> 提供」安全提示；同源服務的沙箱是假的（見 IframeOverlay
//     註解），改開新分頁並告知使用者。
// legacy platform_links 無 launch 端點，直接以既有 url 於新分頁開啟。
//
// 每一次點擊都必須留下痕跡：成功 → role="status" 一行，失敗 → role="alert" ＋ toast。
// 「按了、沒報錯、什麼也沒發生」是本專案定義的最壞失效模式（2026-08-02 六張卡有
// 五張如此），不得再出現。
//
// 尚未與任務（Task）耦合——Slice 9 才會接 task→service。

import React, { useCallback, useEffect, useState } from "react";

import { authRequest } from "./runtime/api.js";
import {
  IconBook,
  IconExternal,
  IconFile,
  IconFolder,
  IconGauge,
  IconGrid,
  IconLink,
  IconMessage,
  IconNodes,
  IconRoute,
  IconShield,
  IconSpark,
  IconTerminal,
  IconX,
} from "./icons.jsx";
import {
  launchFailureNotice,
  newTabOpenedNotice,
  sameOriginOpenedInNewTabNotice,
} from "./uxCopy.js";

/**
 * Fallback icon for unknown `service.icon` keys.
 * Lookups must never throw — governance may add keys before the SPA ships them.
 */
const SERVICE_ICON_FALLBACK = IconSpark;

/** Server allow-list keys → SPA icon components (see schemas.service_icon.ALLOWED_SERVICE_ICONS). */
const SERVICE_ICONS = {
  workflow: IconRoute,
  git: IconNodes,
  notebook: IconBook,
  chat: IconMessage,
  monitor: IconGauge,
  database: IconFolder,
  api: IconLink,
  docs: IconFile,
  cpu: IconTerminal,
};

/** Resolve an icon component; unknown keys → SERVICE_ICON_FALLBACK (never throws). */
function resolveServiceIcon(key) {
  // Own-enumerable string keys only. A plain `map[key] || fallback` is truthy for
  // prototype names (constructor / valueOf / __proto__ / …) and React then throws
  // "Element type is invalid" — anila-shell has no ErrorBoundary.
  if (typeof key !== "string" || !Object.hasOwn(SERVICE_ICONS, key)) {
    return SERVICE_ICON_FALLBACK;
  }
  return SERVICE_ICONS[key];
}

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

/**
 * launch_url 是否指向本平台自己的 origin。
 *
 * 平台自營的服務(/anila、/n8n、/codeserver…)由同一台 nginx
 * 同源代理,後端回的 launch_url 就是相對路徑。這件事對「內嵌」模式有決定性
 * 影響 —— 見 IframeOverlay 的註解。
 *
 * 解析不出來(空字串、畸形 URL)一律當**同源**:那是保守的一邊,只會少開一個
 * iframe,不會反過來讓一個沙箱失效的 frame 被當成有沙箱。
 */
export function isSameOriginUrl(url) {
  if (typeof window === "undefined" || !window.location) return true;
  try {
    return new URL(url, window.location.href).origin === window.location.origin;
  } catch {
    return true;
  }
}

// ---- UI 層 ------------------------------------------------------------------

function ServiceCard({ service, onLaunch }) {
  const ServiceIcon = resolveServiceIcon(service.icon);
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
        <div
          data-service-icon={service.icon || ""}
          style={{
          width: 34, height: 34, borderRadius: "var(--radius)",
          background: "var(--bg-subtle)", border: "1px solid var(--border)",
          display: "inline-flex", alignItems: "center", justifyContent: "center",
          color: "var(--fg-muted)",
        }}><ServiceIcon size={18} /></div>
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
      {/*
        sandbox 這三個 token 為什麼是這三個,以及為什麼這個 overlay 只給
        **跨 origin** 的服務用:

        `allow-scripts` + `allow-same-origin` 放在一起,只有在框進來的文件與
        外層**同源**時才會互相抵銷 —— 那時 frame 拿得到 parent 的 document,
        可以直接把自己的 sandbox 屬性拿掉,沙箱等於不存在。跨 origin 就不是
        這回事:`allow-same-origin` 只是讓 frame 保有**它自己的** origin
        (沒有它 frame 會拿到不透明 origin,連自己的 cookie / storage 都讀不到,
        SSO 直接壞掉),它拿不到我們的 document。

        所以跨 origin 時這個 sandbox 是真的有在擋事:預設就否決了彈窗、
        頂層導覽(把使用者整頁換掉)、下載、modal、pointer lock、
        popups-to-escape-sandbox。同源時它一項都擋不住 —— 因此
        handleLaunch 不會讓同源服務走到這裡,改開新分頁並明講。
        橫幅那句「內容於受限沙箱中執行」是給使用者的保證,不能有例外。
      */}
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
  // { text, detail } | null —— text 是使用者讀的那句,detail 是後端原文
  // (留給管理員轉述用,不當第一行)。
  const [launchError, setLaunchError] = useState(null);
  // 成功那一側的回饋(字串)。失敗與成功各佔一行,不互相蓋掉。
  const [launchNotice, setLaunchNotice] = useState("");

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
    setLaunchError(null);
    setLaunchNotice("");
    setIframe(null);
    void load();
  }, [open, load]);

  // 抽屜開著時 Escape 關閉——跟設定、分享、附件檢視一樣；aria-modal 的對話框不吃
  // Escape，鍵盤使用者只能用滑鼠找右上角的叉（逐頁走查 2026-09-02）。
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => {
      if (e.key === "Escape") onClose?.();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  // 失敗一律同時走兩個出口:抽屜頂端的 role="alert"(留在畫面上、可以再讀一次)
  // + toast(蓋在畫面上、一定會被看到)。只寫 console.error 等於沒說。
  function fail(service, err, textOverride) {
    const text = textOverride || launchFailureNotice(service?.name, err);
    // 後端 detail 與使用者那句不同時才附上;相同就不重複同一句話。
    const detail = err?.message && err.message !== text ? err.message : "";
    setLaunchNotice("");
    setLaunchError({ text, detail });
    toast?.(text, { tone: "error" });
  }

  function openNewTab(service, url) {
    // 保留 noopener(瀏覽器層級的保護,不為了偵測而拿掉)。代價是回傳值
    // 依規格恆為 null,前端分不出「開了」與「被擋」——所以不猜,改成每次
    // 都給一行回饋,把使用者自己查得到的那一步講出來(見 uxCopy 註解)。
    window.open(url, "_blank", "noopener");
    setLaunchNotice(newTabOpenedNotice(service?.name));
  }

  async function handleLaunch(service) {
    setLaunchError(null);
    setLaunchNotice("");
    try {
      const { mode, launch_url } = await resolveLaunch(request, service);
      if (!launch_url) {
        fail(service, null, launchFailureNotice(service?.name, { status: 400 }));
        return;
      }
      if (mode === "iframe" && !isSameOriginUrl(launch_url)) {
        setIframe({ url: launch_url, name: service.name });
        return;
      }
      if (mode === "iframe") {
        // 同源內容沙箱不了(見 IframeOverlay 註解),不掛假保證,改開新分頁
        // 並且明白告訴使用者為什麼跟他設定的不一樣。
        toast?.(sameOriginOpenedInNewTabNotice(service?.name), { tone: "info" });
      }
      openNewTab(service, launch_url);
    } catch (err) {
      fail(service, err);
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
            }}>
              <div>{launchError.text}</div>
              {/* 後端原文:管理員要照這句去查,但它不是使用者的詞,所以壓小、放第二行。 */}
              {launchError.detail && (
                <div data-launch-error-detail="" style={{
                  marginTop: 4, fontSize: 11, opacity: 0.75,
                  fontFamily: "var(--font-mono)", wordBreak: "break-word",
                }}>技術訊息：{launchError.detail}</div>
              )}
            </div>
          )}

          {/* 成功那一側:每次點擊都有回饋,所以「按了什麼也沒發生」不存在。
              role="status" 而非 alert —— 這不是錯誤,不該打斷螢幕閱讀器。 */}
          {launchNotice && (
            <div role="status" style={{
              marginBottom: 14, padding: "8px 12px",
              background: "var(--bg-subtle)", border: "1px solid var(--border)",
              borderRadius: "var(--radius)", color: "var(--fg-muted)", fontSize: 12,
            }}>{launchNotice}</div>
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
