// 共用建議選單 —— `@` agent mention 與 `/` 斜線指令共用同一個元件
// (參考 Open WebUI 的 CommandSuggestionList:一個選單 + 依觸發字元分派候選)。
//
// 樣式逐字沿用原本 chat.jsx 裡的 `@` 選單(inline style,本專案無 CSS 檔),
// 所以 `@` 的外觀零變化。
import React from "react";

/**
 * @param {object}   props
 * @param {string}   props.header        選單頂部說明列
 * @param {string}   props.footer        選單底部操作提示
 * @param {Array}    props.items         [{ key, lead, primary, secondary }]
 * @param {number}   props.activeIndex   目前 highlight 的索引
 * @param {Function} props.onPick        (item, index) => void
 * @param {Function} [props.onHover]     (index) => void
 * @param {string}   [props.ariaLabel]
 */
export const CommandSuggestionList = ({
  header,
  footer,
  items,
  activeIndex,
  onPick,
  onHover,
  ariaLabel = "建議清單",
}) => {
  if (!items || items.length === 0) return null;
  return (
    <div
      role="listbox"
      aria-label={ariaLabel}
      style={{
        position: "absolute",
        bottom: "100%",
        left: 8,
        marginBottom: 6,
        background: "var(--bg-elev)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        boxShadow: "0 12px 32px -8px oklch(0.10 0 0 / 0.18)",
        padding: 4,
        minWidth: 260,
        maxWidth: 420,
        zIndex: 80,
      }}
    >
      {header && (
        <div style={{
          padding: "4px 8px", fontSize: 10, color: "var(--fg-subtle)",
          fontFamily: "var(--font-mono)", letterSpacing: 0.4,
        }}>
          {header}
        </div>
      )}
      {items.map((item, i) => (
        <button
          key={item.key}
          type="button"
          role="option"
          aria-selected={i === activeIndex}
          onMouseDown={(e) => { e.preventDefault(); onPick(item, i); }}
          onMouseEnter={() => onHover?.(i)}
          style={{
            display: "flex", alignItems: "center", gap: 8, width: "100%",
            padding: "6px 8px",
            background: i === activeIndex ? "var(--bg-subtle)" : "transparent",
            border: "none", borderRadius: 4,
            color: "var(--fg)", textAlign: "left", cursor: "pointer",
            fontSize: 12, fontFamily: "inherit",
          }}
        >
          <span style={{
            fontFamily: "var(--font-mono)",
            color: "var(--accent)",
            fontSize: 11,
            minWidth: 48,
          }}>{item.lead}</span>
          <span style={{
            flex: 1, minWidth: 0, overflow: "hidden",
            textOverflow: "ellipsis", whiteSpace: "nowrap",
          }}>{item.primary}</span>
          {item.secondary && (
            <span style={{
              fontSize: 10, color: "var(--fg-subtle)",
              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
              maxWidth: 170,
            }}>{item.secondary}</span>
          )}
        </button>
      ))}
      {footer && (
        <div style={{
          padding: "4px 8px", fontSize: 10, color: "var(--fg-subtle)",
          borderTop: "1px solid var(--border)", marginTop: 2,
        }}>
          {footer}
        </div>
      )}
    </div>
  );
};
