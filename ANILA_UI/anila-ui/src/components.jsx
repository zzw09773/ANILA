// Shared low-level components

const { useState, useEffect, useRef, useMemo, useCallback } = React;

// Button
const Button = ({ variant = "default", size = "md", children, leftIcon, rightIcon, className = "", ...rest }) => {
  const base = {
    display: "inline-flex", alignItems: "center", gap: 6,
    border: "1px solid transparent",
    borderRadius: "var(--radius)",
    fontWeight: 500,
    fontSize: size === "sm" ? 12 : 13,
    padding: size === "sm" ? "4px 10px" : size === "lg" ? "9px 16px" : "6px 12px",
    transition: "all .12s ease",
    whiteSpace: "nowrap",
    userSelect: "none",
    background: "transparent",
    color: "var(--fg)",
  };
  const variants = {
    default: { background: "var(--bg-elev)", borderColor: "var(--border)" },
    primary: { background: "var(--accent)", color: "var(--accent-fg)", borderColor: "var(--accent)" },
    ghost:   { background: "transparent", color: "var(--fg-muted)" },
    subtle:  { background: "var(--bg-subtle)", borderColor: "var(--border)" },
    danger:  { background: "transparent", color: "var(--danger)", borderColor: "var(--border)" },
  };
  return (
    <button {...rest} className={className}
      style={{ ...base, ...variants[variant], ...(rest.style || {}) }}
      onMouseEnter={e => { if(variant!=="primary") e.currentTarget.style.background = "var(--bg-subtle)"; }}
      onMouseLeave={e => { Object.assign(e.currentTarget.style, variants[variant]); }}
    >
      {leftIcon}{children}{rightIcon}
    </button>
  );
};

// Icon-only button
const IconButton = ({ children, active, title, className = "", ...rest }) => (
  <button {...rest} title={title} className={className} style={{
    display: "inline-flex", alignItems: "center", justifyContent: "center",
    width: 30, height: 30,
    background: active ? "var(--bg-subtle)" : "transparent",
    color: active ? "var(--fg)" : "var(--fg-muted)",
    border: "1px solid " + (active ? "var(--border)" : "transparent"),
    borderRadius: "var(--radius)",
    cursor: "pointer",
    transition: "all .12s ease",
    ...(rest.style || {}),
  }}
    onMouseEnter={e => { e.currentTarget.style.background = "var(--bg-subtle)"; e.currentTarget.style.color = "var(--fg)"; }}
    onMouseLeave={e => {
      e.currentTarget.style.background = active ? "var(--bg-subtle)" : "transparent";
      e.currentTarget.style.color = active ? "var(--fg)" : "var(--fg-muted)";
    }}
  >{children}</button>
);

// Agent badge / pill
const AgentPill = ({ agent, size = "md", selected }) => {
  if (!agent) return null;
  const s = size === "sm";
  return (
    <div style={{
      display: "inline-flex", alignItems: "center", gap: 6,
      padding: s ? "2px 7px" : "3px 9px",
      fontSize: s ? 11 : 12,
      fontFamily: "var(--font-mono)",
      fontWeight: 500,
      background: selected ? "var(--accent-soft)" : "var(--bg-subtle)",
      color: "var(--fg)",
      border: "1px solid " + (selected ? "var(--accent)" : "var(--border)"),
      borderRadius: 999,
      lineHeight: 1.3,
    }}>
      <span style={{
        width: s ? 5 : 6, height: s ? 5 : 6, borderRadius: 999,
        background: agent.id === "anila-router" ? "var(--accent)" : "var(--fg-muted)",
      }}/>
      {agent.short || agent.id}
    </div>
  );
};

// Kbd
const Kbd = ({ children }) => (
  <span style={{
    fontFamily: "var(--font-mono)", fontSize: 10.5,
    padding: "1px 5px",
    background: "var(--bg)", border: "1px solid var(--border)",
    borderRadius: 4, color: "var(--fg-muted)",
  }}>{children}</span>
);

// Thin divider
const Divider = ({ vertical, style = {} }) => (
  <div style={{
    background: "var(--border)",
    ...(vertical ? { width: 1, alignSelf: "stretch" } : { height: 1, width: "100%" }),
    ...style,
  }}/>
);

// Dropdown menu
const Dropdown = ({ trigger, children, align = "left", width = 280, maxHeight = 360 }) => {
  const [open, setOpen] = useState(false);
  const [placement, setPlacement] = useState("bottom"); // bottom | top
  const ref = useRef(null);
  const triggerRef = useRef(null);

  useEffect(() => {
    if (!open) return;
    // Decide placement based on available viewport space below the trigger
    if (triggerRef.current) {
      const rect = triggerRef.current.getBoundingClientRect();
      const spaceBelow = window.innerHeight - rect.bottom;
      const spaceAbove = rect.top;
      if (spaceBelow < maxHeight && spaceAbove > spaceBelow) {
        setPlacement("top");
      } else {
        setPlacement("bottom");
      }
    }
    const h = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [open, maxHeight]);

  const panelPos = placement === "top"
    ? { bottom: "calc(100% + 4px)", [align]: 0 }
    : { top: "calc(100% + 4px)", [align]: 0 };

  return (
    <div ref={ref} style={{ position: "relative", display: "inline-block" }}>
      <div ref={triggerRef} onClick={() => setOpen(o => !o)}>{trigger(open)}</div>
      {open && (
        <div style={{
          position: "absolute", ...panelPos,
          zIndex: 80,
          minWidth: width,
          maxHeight,
          overflowY: "auto",
          background: "var(--bg-elev)",
          border: "1px solid var(--border)",
          borderRadius: "var(--radius-lg)",
          boxShadow: "0 12px 32px -8px oklch(0.10 0 0 / 0.18), 0 0 0 1px oklch(0.10 0 0 / 0.02)",
          padding: 4,
        }}>{typeof children === "function" ? children(() => setOpen(false)) : children}</div>
      )}
    </div>
  );
};

const MenuItem = ({ children, leftIcon, rightIcon, active, onClick, className = "" }) => (
  <button className={className} onClick={onClick} style={{
    display: "flex", alignItems: "center", gap: 10, width: "100%",
    padding: "8px 10px",
    background: active ? "var(--bg-subtle)" : "transparent",
    border: "none", borderRadius: "var(--radius)",
    color: "var(--fg)", textAlign: "left", cursor: "pointer",
    fontSize: 13,
  }}
    onMouseEnter={e => { e.currentTarget.style.background = "var(--bg-subtle)"; }}
    onMouseLeave={e => { e.currentTarget.style.background = active ? "var(--bg-subtle)" : "transparent"; }}>
    {leftIcon}
    <span style={{ flex: 1, minWidth: 0 }}>{children}</span>
    {rightIcon}
  </button>
);

// Modal
const Modal = ({ open, onClose, title, subtitle, children, width = 480 }) => {
  useEffect(() => {
    if (!open) return;
    const h = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div onClick={onClose} style={{
      position: "fixed", inset: 0, zIndex: 100,
      background: "oklch(0.10 0 0 / 0.4)",
      display: "flex", alignItems: "center", justifyContent: "center",
      padding: 20,
    }}>
      <div onClick={e => e.stopPropagation()} style={{
        background: "var(--bg-elev)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius-lg)",
        width: "100%", maxWidth: width,
        boxShadow: "0 24px 60px -20px oklch(0.10 0 0 / 0.35)",
        overflow: "hidden",
      }}>
        <div style={{
          padding: "16px 20px 12px",
          borderBottom: "1px solid var(--border)",
          display: "flex", alignItems: "start", justifyContent: "space-between", gap: 16,
        }}>
          <div>
            <div style={{ fontSize: 15, fontWeight: 600 }}>{title}</div>
            {subtitle && <div style={{ fontSize: 12, color: "var(--fg-muted)", marginTop: 2 }}>{subtitle}</div>}
          </div>
          <IconButton onClick={onClose}><IconX/></IconButton>
        </div>
        <div style={{ padding: 20 }}>{children}</div>
      </div>
    </div>
  );
};

// Input
const Input = ({ label, hint, error, leftIcon, rightEl, ...rest }) => (
  <label style={{ display: "block" }}>
    {label && <div style={{ fontSize: 12, fontWeight: 500, color: "var(--fg-muted)", marginBottom: 6 }}>{label}</div>}
    <div style={{
      display: "flex", alignItems: "center",
      background: "var(--bg-elev)",
      border: "1px solid " + (error ? "var(--danger)" : "var(--border)"),
      borderRadius: "var(--radius)",
      transition: "border-color .12s",
    }}
    onFocusCapture={e => { if (!error) e.currentTarget.style.borderColor = "var(--accent)"; }}
    onBlurCapture={e => { if (!error) e.currentTarget.style.borderColor = "var(--border)"; }}>
      {leftIcon && <span style={{ color: "var(--fg-subtle)", paddingLeft: 10, display: "flex" }}>{leftIcon}</span>}
      <input {...rest} style={{
        flex: 1, minWidth: 0,
        background: "transparent", border: "none", outline: "none",
        padding: "8px 10px", fontSize: 13, color: "var(--fg)",
      }}/>
      {rightEl && <span style={{ paddingRight: 4, display: "flex" }}>{rightEl}</span>}
    </div>
    {error && <div style={{ fontSize: 11, color: "var(--danger)", marginTop: 4 }}>{error}</div>}
    {hint && !error && <div style={{ fontSize: 11, color: "var(--fg-subtle)", marginTop: 4 }}>{hint}</div>}
  </label>
);

Object.assign(window, {
  Button, IconButton, AgentPill, Kbd, Divider, Dropdown, MenuItem, Modal, Input,
});
