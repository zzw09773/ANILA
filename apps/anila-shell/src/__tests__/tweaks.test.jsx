import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { TweaksPanel } from "../tweaks.jsx";
import { applyTweaks, DEFAULT_TWEAKS, resolveInitialTweaks } from "../runtime/tweaksApply.js";

afterEach(() => {
  delete window.ANILA_TWEAKS;
  applyTweaks(DEFAULT_TWEAKS);
});

describe("外觀設定相容性", () => {
  it("移除位置設定，舊未知 key 不影響其他設定的套用與更新", () => {
    expect(DEFAULT_TWEAKS).not.toHaveProperty("agentSwitcherPosition");
    window.ANILA_TWEAKS = { agentSwitcherPosition: "top", density: 20, dark: true };
    const tweaks = resolveInitialTweaks();
    expect(() => applyTweaks(tweaks)).not.toThrow();
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(document.documentElement.style.getPropertyValue("--density")).toBe("20px");
    const setTweaks = vi.fn();
    render(<TweaksPanel open onClose={vi.fn()} tweaks={tweaks} setTweaks={setTweaks} />);
    expect(screen.queryByText("AGENT SWITCHER 位置")).toBeNull();
    expect(screen.queryByRole("button", { name: "頂部" })).toBeNull();
    expect(screen.queryByRole("button", { name: "靠近輸入框" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "淺色" }));
    expect(setTweaks).toHaveBeenCalledWith({ ...tweaks, dark: false });
    expect(screen.getByRole("button", { name: "可收合" })).toBeTruthy();
  });
});
