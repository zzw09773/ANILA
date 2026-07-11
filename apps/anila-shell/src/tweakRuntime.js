/** Keep legacy shell variables and shared design-system tokens in sync. */
export function applyTweaks(tweaks) {
  const root = document.documentElement;
  root.setAttribute("data-theme", tweaks.dark ? "dark" : "light");

  if (tweaks.accent) {
    root.style.setProperty("--accent", tweaks.accent);
    root.style.setProperty("--accent-fg", "#fff");
    root.style.setProperty("--anila-color-accent", tweaks.accent);
    root.style.setProperty("--anila-color-accent-fg", "#fff");
  }
  if (tweaks.density) {
    root.style.setProperty("--density", `${tweaks.density}px`);
  }
  if (tweaks.sansFamily) {
    const sans = `"${tweaks.sansFamily}", "Inter", system-ui, sans-serif`;
    root.style.setProperty("--font-sans", sans);
    root.style.setProperty("--anila-font-sans", sans);
  }
  if (tweaks.monoFamily) {
    const mono = `"${tweaks.monoFamily}", ui-monospace, Menlo, monospace`;
    root.style.setProperty("--font-mono", mono);
    root.style.setProperty("--anila-font-mono", mono);
  }
}
