// ANILA LM Redesign — main app
// Renders all screens inside a Design Canvas for side-by-side comparison.
// Theme (dark/light) is controlled per-artboard via a `theme` prop.

const { useState, useEffect, useRef, useMemo } = React;

// ─── Tokens ─────────────────────────────────────────────────────────
const tokens = {
  dark: {
    bg: "#0B0D10",
    surface: "#13161B",
    surface2: "#191D24",
    elevated: "#20242C",
    border: "#262B34",
    borderStrong: "#323844",
    text: "#E8EAED",
    textMuted: "#9AA3AE",
    textSubtle: "#6B7280",
    accent: "#7C7BFF",
    accentHover: "#8E8DFF",
    accentSoft: "rgba(124,123,255,0.14)",
    accentBorder: "rgba(124,123,255,0.32)",
    success: "#3DD68C",
    warning: "#F4B740",
    danger: "#FF6B6B",
    chipBg: "#1A1F27",
  },
  light: {
    bg: "#FAFAF7",
    surface: "#FFFFFF",
    surface2: "#F5F5F0",
    elevated: "#FFFFFF",
    border: "#E8E6DF",
    borderStrong: "#D4D2CB",
    text: "#1A1A1A",
    textMuted: "#5C6470",
    textSubtle: "#8B919C",
    accent: "#5957E8",
    accentHover: "#4A48D6",
    accentSoft: "rgba(89,87,232,0.10)",
    accentBorder: "rgba(89,87,232,0.28)",
    success: "#2BB673",
    warning: "#D89B1F",
    danger: "#E5484D",
    chipBg: "#F2F1EB",
  },
};

const useThemeVars = (theme) => useMemo(() => tokens[theme], [theme]);

// ─── Tiny icon set (inline SVG) ─────────────────────────────────────
const Icon = ({ name, size = 16, stroke = "currentColor", strokeWidth = 1.75, fill = "none", style }) => {
  const props = {
    width: size, height: size, viewBox: "0 0 24 24",
    fill, stroke, strokeWidth, strokeLinecap: "round", strokeLinejoin: "round",
    style,
  };
  const paths = {
    book:        <><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" /></>,
    plus:        <><path d="M12 5v14M5 12h14" /></>,
    search:      <><circle cx="11" cy="11" r="7" /><path d="m21 21-4.3-4.3" /></>,
    settings:    <><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></>,
    user:        <><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" /><circle cx="12" cy="7" r="4" /></>,
    trash:       <><polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" /></>,
    logout:      <><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><polyline points="16 17 21 12 16 7" /><line x1="21" y1="12" x2="9" y2="12" /></>,
    file:        <><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /></>,
    upload:      <><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="17 8 12 3 7 8" /><line x1="12" y1="3" x2="12" y2="15" /></>,
    chevL:       <><polyline points="15 18 9 12 15 6" /></>,
    chevR:       <><polyline points="9 18 15 12 9 6" /></>,
    chevD:       <><polyline points="6 9 12 15 18 9" /></>,
    msg:         <><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></>,
    send:        <><line x1="22" y1="2" x2="11" y2="13" /><polygon points="22 2 15 22 11 13 2 9 22 2" /></>,
    sparkle:     <><path d="M12 3l1.9 5.5L19 10l-5.1 1.5L12 17l-1.9-5.5L5 10l5.1-1.5z" /><path d="M19 3v3M21 4.5h-3M5 17v3M6.5 18.5h-3" /></>,
    layers:      <><polygon points="12 2 2 7 12 12 22 7 12 2" /><polyline points="2 17 12 22 22 17" /><polyline points="2 12 12 17 22 12" /></>,
    sun:         <><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" /></>,
    moon:        <><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" /></>,
    panel:       <><rect x="3" y="3" width="18" height="18" rx="2" /><path d="M9 3v18" /></>,
    mic:         <><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" /><path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v4M8 23h8" /></>,
    deck:        <><rect x="2" y="3" width="20" height="14" rx="2" /><line x1="8" y1="21" x2="16" y2="21" /><line x1="12" y1="17" x2="12" y2="21" /></>,
    video:       <><polygon points="23 7 16 12 23 17 23 7" /><rect x="1" y="5" width="15" height="14" rx="2" /></>,
    git:         <><circle cx="6" cy="6" r="2" /><circle cx="18" cy="6" r="2" /><circle cx="12" cy="18" r="2" /><path d="M6 8v3a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V8M12 13v3" /></>,
    flash:       <><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" /></>,
    quiz:        <><circle cx="12" cy="12" r="10" /><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3M12 17h.01" /></>,
    chart:       <><line x1="12" y1="20" x2="12" y2="10" /><line x1="18" y1="20" x2="18" y2="4" /><line x1="6" y1="20" x2="6" y2="16" /></>,
    table:       <><rect x="3" y="3" width="18" height="18" rx="2" /><path d="M3 9h18M3 15h18M9 3v18M15 3v18" /></>,
    edit:        <><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" /><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" /></>,
    quote:       <><path d="M3 21c3-1 5-3 5-7V5H3v9h3M21 21c3-1 5-3 5-7V5h-5v9h3" transform="translate(-4 0)" /></>,
    paperclip:   <><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" /></>,
    check:       <><polyline points="20 6 9 17 4 12" /></>,
    grid:        <><rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" /><rect x="14" y="14" width="7" height="7" /><rect x="3" y="14" width="7" height="7" /></>,
    arrowR:      <><line x1="5" y1="12" x2="19" y2="12" /><polyline points="12 5 19 12 12 19" /></>,
    folder:      <><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" /></>,
    pin:         <><line x1="12" y1="17" x2="12" y2="22" /><path d="M5 17h14l-1.5-9h-11z" /><path d="M9 8V3h6v5" /></>,
  };
  return <svg {...props}>{paths[name]}</svg>;
};

// ─── Theme toggle ──────────────────────────────────────────────────
const ThemeSwitch = ({ theme, setTheme, t }) => (
  <button
    onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
    aria-label="Toggle theme"
    style={{
      display: "inline-flex", alignItems: "center", gap: 6,
      padding: "6px 10px", borderRadius: 999,
      background: t.surface2, border: `1px solid ${t.border}`,
      color: t.textMuted, cursor: "pointer", fontSize: 12,
    }}
  >
    <Icon name={theme === "dark" ? "moon" : "sun"} size={14} />
    <span style={{ color: t.text, fontWeight: 500 }}>{theme === "dark" ? "深色" : "淺色"}</span>
  </button>
);

// expose to other files
window.tokens = tokens;
window.useThemeVars = useThemeVars;
window.Icon = Icon;
window.ThemeSwitch = ThemeSwitch;
