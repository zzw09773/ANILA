// 集中式 shortcuts registry —— 鍵位比對(跨平台 mod)、顯示字串、分組產生。
import { describe, it, expect } from "vitest";

import {
  SHORTCUTS,
  SHORTCUT_GROUPS,
  formatChord,
  formatShortcut,
  getShortcut,
  globalShortcuts,
  groupedShortcuts,
  isMacPlatform,
  matchesChord,
  resolveGlobalShortcut,
} from "../commands/shortcuts.js";

const evt = (over = {}) => ({
  key: "k",
  metaKey: false,
  ctrlKey: false,
  shiftKey: false,
  altKey: false,
  ...over,
});

describe("isMacPlatform", () => {
  it("detects macOS / iOS user agents", () => {
    expect(isMacPlatform({ platform: "MacIntel" })).toBe(true);
    expect(isMacPlatform({ userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)" })).toBe(true);
  });

  it("treats everything else as non-mac", () => {
    expect(isMacPlatform({ platform: "Win32" })).toBe(false);
    expect(isMacPlatform({ platform: "Linux x86_64" })).toBe(false);
    expect(isMacPlatform({})).toBe(false);
    expect(isMacPlatform(null)).toBe(false);
  });
});

describe("formatChord / formatShortcut", () => {
  it("renders ⌘ on macOS and Ctrl elsewhere", () => {
    const chord = { mod: true, key: "k" };
    expect(formatChord(chord, { mac: true })).toBe("⌘K");
    expect(formatChord(chord, { mac: false })).toBe("Ctrl+K");
  });

  it("renders shift / alt modifiers per platform", () => {
    const chord = { mod: true, shift: true, key: "o" };
    expect(formatChord(chord, { mac: true })).toBe("⌘⇧O");
    expect(formatChord(chord, { mac: false })).toBe("Ctrl+Shift+O");
    expect(formatChord({ alt: true, key: "a" }, { mac: true })).toBe("⌥A");
    expect(formatChord({ alt: true, key: "a" }, { mac: false })).toBe("Alt+A");
  });

  it("maps special keys to readable glyphs", () => {
    expect(formatChord({ key: "ArrowUp" }, { mac: false })).toBe("↑");
    expect(formatChord({ key: "Escape" }, { mac: false })).toBe("Esc");
    expect(formatChord({ key: "Enter" }, { mac: false })).toBe("Enter");
  });

  it("joins alternative chords with a slash", () => {
    expect(formatShortcut(getShortcut("suggest-accept"), { mac: false })).toBe("Enter / Tab");
    expect(formatShortcut(getShortcut("suggest-move"), { mac: false })).toBe("↑ / ↓");
  });

  it("is null-safe", () => {
    expect(formatShortcut(null)).toBe("");
    expect(formatChord(null)).toBe("");
  });
});

describe("matchesChord", () => {
  it("maps mod → metaKey on macOS", () => {
    const chord = { mod: true, key: "k" };
    expect(matchesChord(evt({ metaKey: true }), chord, { mac: true })).toBe(true);
    expect(matchesChord(evt({ ctrlKey: true }), chord, { mac: true })).toBe(false);
  });

  it("maps mod → ctrlKey off macOS", () => {
    const chord = { mod: true, key: "k" };
    expect(matchesChord(evt({ ctrlKey: true }), chord, { mac: false })).toBe(true);
    expect(matchesChord(evt({ metaKey: true }), chord, { mac: false })).toBe(false);
  });

  it("rejects when the other platform modifier is also held", () => {
    expect(
      matchesChord(evt({ ctrlKey: true, metaKey: true }), { mod: true, key: "k" }, { mac: false }),
    ).toBe(false);
  });

  it("is case-insensitive on the key", () => {
    expect(matchesChord(evt({ key: "K", ctrlKey: true }), { mod: true, key: "k" }, { mac: false })).toBe(true);
  });

  it("honours the shift requirement unless allowShift is set", () => {
    const strict = { mod: true, shift: true, key: "o" };
    expect(matchesChord(evt({ key: "o", ctrlKey: true, shiftKey: true }), strict, { mac: false })).toBe(true);
    expect(matchesChord(evt({ key: "o", ctrlKey: true }), strict, { mac: false })).toBe(false);

    const lenient = { mod: true, key: "/", allowShift: true };
    expect(matchesChord(evt({ key: "/", ctrlKey: true }), lenient, { mac: false })).toBe(true);
    expect(matchesChord(evt({ key: "/", ctrlKey: true, shiftKey: true }), lenient, { mac: false })).toBe(true);
  });

  it("does not fire a plain key when a mod is required", () => {
    expect(matchesChord(evt({ key: "k" }), { mod: true, key: "k" }, { mac: false })).toBe(false);
  });

  // 瀏覽器的 event.key 是「產生的字元」而不是實體鍵位:按 Shift+/ 回報的是
  // "?" 而不是 "/"。原本只比對 chord.key,宣稱支援的 ⌘? 其實從來沒生效。
  it("aliasKeys 讓同一個實體鍵位在不同 modifier 下也能命中", () => {
    const chord = { mod: true, key: "/", allowShift: true, aliasKeys: ["?"] };
    expect(matchesChord(evt({ key: "?", ctrlKey: true, shiftKey: true }), chord, { mac: false })).toBe(true);
    expect(matchesChord(evt({ key: "/", ctrlKey: true }), chord, { mac: false })).toBe(true);
    // 沒列進 aliasKeys 的鍵不會誤觸。
    expect(matchesChord(evt({ key: "\\", ctrlKey: true }), chord, { mac: false })).toBe(false);
  });
});

describe("resolveGlobalShortcut", () => {
  it("resolves ⌘K / Ctrl+K to the command palette", () => {
    expect(resolveGlobalShortcut(evt({ ctrlKey: true }), { mac: false })?.id).toBe("command-palette");
    expect(resolveGlobalShortcut(evt({ metaKey: true }), { mac: true })?.id).toBe("command-palette");
  });

  it("resolves ⌘/ to the shortcuts panel", () => {
    expect(
      resolveGlobalShortcut(evt({ key: "/", ctrlKey: true }), { mac: false })?.id,
    ).toBe("shortcuts-panel");
  });

  it("resolves ⌘? — 瀏覽器對 Shift+/ 回報的是 key=\"?\"(真實鍵盤行為)", () => {
    expect(
      resolveGlobalShortcut(evt({ key: "?", ctrlKey: true, shiftKey: true }), { mac: false })?.id,
    ).toBe("shortcuts-panel");
    expect(
      resolveGlobalShortcut(evt({ key: "?", metaKey: true, shiftKey: true }), { mac: true })?.id,
    ).toBe("shortcuts-panel");
  });

  it("resolves ⌘⇧O to new chat", () => {
    expect(
      resolveGlobalShortcut(evt({ key: "o", ctrlKey: true, shiftKey: true }), { mac: false })?.id,
    ).toBe("new-chat");
  });

  it("never claims bare Enter / Escape (區域行為不可被搶)", () => {
    expect(resolveGlobalShortcut(evt({ key: "Enter" }), { mac: false })).toBeNull();
    expect(resolveGlobalShortcut(evt({ key: "Escape" }), { mac: false })).toBeNull();
    expect(resolveGlobalShortcut(evt({ key: "Tab" }), { mac: false })).toBeNull();
    expect(resolveGlobalShortcut(evt({ key: "/" }), { mac: false })).toBeNull();
  });
});

describe("registry shape", () => {
  it("only exposes mod-based chords as global (才不會搶掉打字)", () => {
    for (const shortcut of globalShortcuts()) {
      for (const chord of shortcut.chords) {
        expect(chord.mod).toBe(true);
      }
    }
  });

  it("has unique ids and a known group for every entry", () => {
    const ids = SHORTCUTS.map((s) => s.id);
    expect(new Set(ids).size).toBe(ids.length);
    const groups = new Set(SHORTCUT_GROUPS.map((g) => g.id));
    for (const s of SHORTCUTS) {
      expect(groups.has(s.group)).toBe(true);
      expect(s.description.length).toBeGreaterThan(0);
      expect(s.chords.length).toBeGreaterThan(0);
    }
  });

  it("groupedShortcuts covers every registered shortcut", () => {
    const flat = groupedShortcuts().flatMap((g) => g.items);
    expect(flat).toHaveLength(SHORTCUTS.length);
    expect(groupedShortcuts().every((g) => g.items.length > 0)).toBe(true);
  });
});
