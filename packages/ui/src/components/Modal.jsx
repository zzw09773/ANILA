// Modal — 自 shell components.jsx 收編：focus trap（記住/還原焦點、Tab 循環、
// ESC 關閉）逐字保留，樣式改吃 tokens。
import React, { useEffect, useRef } from "react";
import { IconX } from "../icons.jsx";
import { IconButton } from "./Button.jsx";

const MODAL_FOCUSABLE =
  'a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])';

export const Modal = ({ open, onClose, title, subtitle, children, width = 480 }) => {
  const panelRef = useRef(null);
  useEffect(() => {
    if (!open) return;
    // a11y：記住先前焦點、把焦點移入對話框、Tab 循環、關閉時還原焦點。
    const prevFocus = document.activeElement;
    const panel = panelRef.current;
    const initial = panel?.querySelectorAll(MODAL_FOCUSABLE);
    (initial && initial.length ? initial[0] : panel)?.focus();
    const h = (e) => {
      if (e.key === "Escape") {
        onClose();
        return;
      }
      if (e.key === "Tab" && panel) {
        const f = panel.querySelectorAll(MODAL_FOCUSABLE);
        if (!f.length) {
          e.preventDefault();
          return;
        }
        const first = f[0],
          last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    window.addEventListener("keydown", h);
    return () => {
      window.removeEventListener("keydown", h);
      prevFocus?.focus?.();
    };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: "var(--anila-z-modal)",
        background: "oklch(0.10 0 0 / 0.4)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 20,
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === "string" ? title : "對話方塊"}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        style={{
          outline: "none",
          background: "var(--anila-color-bg-elev)",
          border: "1px solid var(--anila-color-border)",
          borderRadius: "var(--anila-radius-lg)",
          width: "100%",
          maxWidth: width,
          boxShadow: "var(--anila-shadow-lg)",
          overflow: "hidden",
          fontFamily: "var(--anila-font-sans)",
        }}
      >
        <div
          style={{
            padding: "16px 20px 12px",
            borderBottom: "1px solid var(--anila-color-border)",
            display: "flex",
            alignItems: "start",
            justifyContent: "space-between",
            gap: 16,
          }}
        >
          <div>
            <div
              style={{
                fontSize: "var(--anila-text-lg)",
                fontWeight: "var(--anila-weight-semibold)",
              }}
            >
              {title}
            </div>
            {subtitle && (
              <div
                style={{
                  fontSize: "var(--anila-text-sm)",
                  color: "var(--anila-color-fg-muted)",
                  marginTop: 2,
                }}
              >
                {subtitle}
              </div>
            )}
          </div>
          <IconButton title="關閉" onClick={onClose}>
            <IconX />
          </IconButton>
        </div>
        <div style={{ padding: 20 }}>{children}</div>
      </div>
    </div>
  );
};
