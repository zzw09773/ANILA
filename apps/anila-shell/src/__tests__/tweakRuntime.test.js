import { afterEach, describe, expect, it } from "vitest";
import { applyTweaks } from "../tweakRuntime.js";

afterEach(() => {
  document.documentElement.removeAttribute("data-theme");
  document.documentElement.removeAttribute("style");
});

describe("applyTweaks", () => {
  it("同步 shell 舊變數與共用設計 token", () => {
    applyTweaks({
      dark: true,
      accent: "#123456",
      density: 18,
      sansFamily: "Noto Sans TC",
      monoFamily: "JetBrains Mono",
    });

    const root = document.documentElement;
    expect(root).toHaveAttribute("data-theme", "dark");
    expect(root.style.getPropertyValue("--accent")).toBe("#123456");
    expect(root.style.getPropertyValue("--anila-color-accent")).toBe("#123456");
    expect(root.style.getPropertyValue("--density")).toBe("18px");
    expect(root.style.getPropertyValue("--font-sans")).toContain("Noto Sans TC");
    expect(root.style.getPropertyValue("--anila-font-sans")).toContain("Noto Sans TC");
    expect(root.style.getPropertyValue("--font-mono")).toContain("JetBrains Mono");
    expect(root.style.getPropertyValue("--anila-font-mono")).toContain("JetBrains Mono");
  });
});
