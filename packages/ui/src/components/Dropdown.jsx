// Dropdown / MenuItem — 自 shell components.jsx 收編：position:fixed +
// viewport clamp（逃離任何 ancestor overflow）邏輯逐字保留。
import React, { useEffect, useRef, useState } from "react";

export const Dropdown = ({
  trigger,
  children,
  align = "left",
  width = 280,
  maxHeight = 360,
}) => {
  const [open, setOpen] = useState(false);
  const [panelStyle, setPanelStyle] = useState(null);
  const ref = useRef(null);
  const triggerRef = useRef(null);

  useEffect(() => {
    if (!open) return;

    const computePosition = () => {
      if (!triggerRef.current) return;
      const rect = triggerRef.current.getBoundingClientRect();
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const margin = 8;

      // 垂直：預設向下；只有下方明顯放不下且上方空間較大才翻上，
      // 翻上時以 bottom 錨定，避免預留 maxHeight 造成大片留白。
      const spaceBelow = vh - rect.bottom;
      const spaceAbove = rect.top;
      const useTop = spaceBelow < maxHeight && spaceAbove > spaceBelow;

      // 水平：從偏好邊起算，再整體 clamp 進 viewport（8px inset）。
      let leftPx;
      if (align === "right") {
        leftPx = rect.right - width;
      } else {
        leftPx = rect.left;
      }
      leftPx = Math.max(margin, Math.min(leftPx, vw - margin - width));

      const baseStyle = {
        position: "fixed",
        left: leftPx,
        width,
        maxHeight,
      };
      if (useTop) {
        setPanelStyle({
          ...baseStyle,
          bottom: Math.max(margin, vh - rect.top + 4),
        });
      } else {
        setPanelStyle({
          ...baseStyle,
          top: Math.min(rect.bottom + 4, vh - margin - 24),
        });
      }
    };

    computePosition();

    const h = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", h);
    const esc = (e) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", esc);
    // 捲動/縮放時讓面板持續錨定 trigger。
    window.addEventListener("resize", computePosition);
    window.addEventListener("scroll", computePosition, true);
    return () => {
      document.removeEventListener("mousedown", h);
      document.removeEventListener("keydown", esc);
      window.removeEventListener("resize", computePosition);
      window.removeEventListener("scroll", computePosition, true);
    };
  }, [open, maxHeight, align, width]);

  return (
    <div ref={ref} style={{ display: "inline-block" }}>
      <div ref={triggerRef} onClick={() => setOpen((o) => !o)}>
        {trigger(open)}
      </div>
      {open && panelStyle && (
        <div
          style={{
            ...panelStyle,
            zIndex: "var(--anila-z-dropdown)",
            overflowY: "auto",
            background: "var(--anila-color-bg-elev)",
            border: "1px solid var(--anila-color-border)",
            borderRadius: "var(--anila-radius-lg)",
            boxShadow: "var(--anila-shadow-md)",
            padding: 4,
          }}
        >
          {typeof children === "function" ? children(() => setOpen(false)) : children}
        </div>
      )}
    </div>
  );
};

export const MenuItem = ({
  children,
  leftIcon,
  rightIcon,
  active,
  onClick,
  className = "",
}) => (
  <button
    className={className}
    onClick={onClick}
    style={{
      display: "flex",
      alignItems: "center",
      gap: 10,
      width: "100%",
      padding: "8px 10px",
      background: active ? "var(--anila-color-bg-subtle)" : "transparent",
      border: "none",
      borderRadius: "var(--anila-radius-md)",
      color: "var(--anila-color-fg)",
      textAlign: "left",
      cursor: "pointer",
      fontSize: "var(--anila-text-md)",
      fontFamily: "var(--anila-font-sans)",
    }}
    onMouseEnter={(e) => {
      e.currentTarget.style.background = "var(--anila-color-bg-subtle)";
    }}
    onMouseLeave={(e) => {
      e.currentTarget.style.background = active
        ? "var(--anila-color-bg-subtle)"
        : "transparent";
    }}
  >
    {leftIcon}
    <span style={{ flex: 1, minWidth: 0 }}>{children}</span>
    {rightIcon}
  </button>
);
