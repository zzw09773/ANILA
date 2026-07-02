// In-app confirm() / toast() built on the shared <Modal>.
//
// Replaces blocking browser dialogs (window.confirm / window.alert) with
// themeable, accessible, non-blocking React UI. The bridge that keeps call
// sites almost identical to the native API is a promise-returning confirm():
//
//   if (!(await confirm("刪除嗎？"))) return;   // was: if (!window.confirm(...)) return;
//   toast("已複製");                              // was: window.alert(...)
//
import React, { createContext, useCallback, useContext, useRef, useState } from "react";
import { Modal, Button } from "./components.jsx";

/**
 * @typedef {Object} ConfirmOptions
 * @property {string} [title]        Dialog heading (default "請確認").
 * @property {string} [message]      Body text. May also be passed as the first arg directly.
 * @property {string} [confirmText]  Confirm button label (default "確定").
 * @property {string} [cancelText]   Cancel button label (default "取消").
 * @property {"danger"|"primary"} [tone]  Confirm button styling (default "primary").
 */

/**
 * @typedef {Object} ConfirmContextValue
 * @property {(opts: string | ConfirmOptions) => Promise<boolean>} confirm
 * @property {(message: string, opts?: { tone?: "info"|"error"|"success", duration?: number }) => void} toast
 */

/** @type {React.Context<ConfirmContextValue | null>} */
const ConfirmContext = createContext(null);

const TOAST_TONE = {
  info:    { border: "var(--border)", fg: "var(--fg)" },
  success: { border: "var(--accent)", fg: "var(--fg)" },
  error:   { border: "var(--danger)", fg: "var(--danger)" },
};

export function ConfirmProvider({ children }) {
  // Single confirm dialog driven by state; resolver lives in a ref so it
  // survives the renders between open() and the user's click.
  const [dialog, setDialog] = useState(null); // null | { ...ConfirmOptions }
  const resolverRef = useRef(null);

  const [toasts, setToasts] = useState([]); // [{ id, message, tone }]
  const toastIdRef = useRef(0);

  const settle = useCallback((value) => {
    const resolve = resolverRef.current;
    resolverRef.current = null;
    setDialog(null);
    if (resolve) resolve(value);
  }, []);

  const confirm = useCallback((opts) => {
    const o = typeof opts === "string" ? { message: opts } : (opts || {});
    return new Promise((resolve) => {
      // If a prior dialog is somehow still open, resolve it false first so we
      // never strand a pending promise.
      if (resolverRef.current) resolverRef.current(false);
      resolverRef.current = resolve;
      setDialog({
        title: o.title || "請確認",
        message: o.message || "",
        confirmText: o.confirmText || "確定",
        cancelText: o.cancelText || "取消",
        tone: o.tone === "danger" ? "danger" : "primary",
      });
    });
  }, []);

  const toast = useCallback((message, opts = {}) => {
    const id = ++toastIdRef.current;
    const tone = opts.tone || "info";
    const duration = opts.duration ?? 3200;
    setToasts((list) => [...list, { id, message, tone }]);
    setTimeout(() => {
      setToasts((list) => list.filter((t) => t.id !== id));
    }, duration);
  }, []);

  const value = useRef(null);
  // Stable identity for the context value object (handlers are already stable).
  if (!value.current) value.current = { confirm, toast };

  return (
    <ConfirmContext.Provider value={value.current}>
      {children}

      <Modal
        open={!!dialog}
        onClose={() => settle(false)}
        title={dialog?.title}
      >
        {dialog && (
          <div>
            <div style={{ fontSize: 13.5, lineHeight: 1.7, color: "var(--fg)", whiteSpace: "pre-wrap" }}>
              {dialog.message}
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 20 }}>
              <Button variant="default" onClick={() => settle(false)}>{dialog.cancelText}</Button>
              <Button variant={dialog.tone} onClick={() => settle(true)}>{dialog.confirmText}</Button>
            </div>
          </div>
        )}
      </Modal>

      {/* Toast stack — bottom-right, non-blocking, auto-dismiss. */}
      <div
        aria-live="polite"
        style={{
          position: "fixed", right: 16, bottom: 16, zIndex: 200,
          display: "flex", flexDirection: "column", gap: 8,
          pointerEvents: "none",
        }}
      >
        {toasts.map((t) => {
          const tone = TOAST_TONE[t.tone] || TOAST_TONE.info;
          return (
            <div
              key={t.id}
              role={t.tone === "error" ? "alert" : "status"}
              style={{
                pointerEvents: "auto",
                maxWidth: 360,
                background: "var(--bg-elev)",
                border: "1px solid " + tone.border,
                borderRadius: "var(--radius)",
                boxShadow: "0 12px 32px -12px oklch(0.10 0 0 / 0.35)",
                padding: "10px 14px",
                fontSize: 13, lineHeight: 1.5,
                color: tone.fg,
                whiteSpace: "pre-wrap",
              }}
            >
              {t.message}
            </div>
          );
        })}
      </div>
    </ConfirmContext.Provider>
  );
}

/** @returns {(opts: string | ConfirmOptions) => Promise<boolean>} */
export function useConfirm() {
  const ctx = useContext(ConfirmContext);
  if (!ctx) throw new Error("useConfirm must be used within <ConfirmProvider>");
  return ctx.confirm;
}

/** @returns {(message: string, opts?: { tone?: "info"|"error"|"success", duration?: number }) => void} */
export function useToast() {
  const ctx = useContext(ConfirmContext);
  if (!ctx) throw new Error("useToast must be used within <ConfirmProvider>");
  return ctx.toast;
}
