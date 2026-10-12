// Appearance tweaks: defaults, initial resolution, and applying them to <html>.

export const DEFAULT_TWEAKS = {
  accent: "#3b5bdb",
  dark: false,
  density: 18,
  sansFamily: "Noto Sans TC",
  monoFamily: "JetBrains Mono",
  agentSwitcherPosition: "bottom",
  traceStyle: "collapsible",
};

export function resolveInitialTweaks() {
  const globalTweaks =
    typeof window !== "undefined" ? window.ANILA_TWEAKS : null;
  return { ...DEFAULT_TWEAKS, ...(globalTweaks || {}) };
}

export function applyTweaks(t) {
  const r = document.documentElement;
  r.setAttribute("data-theme", t.dark ? "dark" : "light");
  if (t.accent && t.accent !== DEFAULT_TWEAKS.accent) {
    r.style.setProperty("--accent", t.accent);
  } else {
    r.style.removeProperty("--accent");
  }
  if (t.density) r.style.setProperty("--density", `${t.density}px`);
  if (t.sansFamily) {
    r.style.setProperty(
      "--font-sans",
      `"${t.sansFamily}", "Inter", system-ui, sans-serif`,
    );
  }
  if (t.monoFamily) {
    r.style.setProperty(
      "--font-mono",
      `"${t.monoFamily}", ui-monospace, Menlo, monospace`,
    );
  }
}
