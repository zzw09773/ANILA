import { describe, it, expect } from "vitest";
import {
  filesFromComposerClipboard,
  isClipboardFilenameOnly,
  namePastedFile,
} from "../runtime/composerPaste.js";

function pngFile(name = "shot.png") {
  return new File([new Uint8Array([137, 80, 78, 71])], name, { type: "image/png" });
}

describe("isClipboardFilenameOnly", () => {
  it("empty or a lone image filename is not real text", () => {
    expect(isClipboardFilenameOnly("")).toBe(true);
    expect(isClipboardFilenameOnly("  ")).toBe(true);
    expect(isClipboardFilenameOnly("photo.PNG")).toBe(true);
    expect(isClipboardFilenameOnly("C:\\\\Users\\\\a\\\\x.webp")).toBe(true);
  });

  it("real sentences stay text", () => {
    expect(isClipboardFilenameOnly("水的化學式")).toBe(false);
    expect(isClipboardFilenameOnly("a.png\nand more")).toBe(false);
  });
});

describe("filesFromComposerClipboard", () => {
  it("attaches an image-only paste", () => {
    const file = pngFile();
    const out = filesFromComposerClipboard({
      items: [{ kind: "file", type: "image/png", getAsFile: () => file }],
      files: [file],
      getData: () => "",
    });
    expect(out).toHaveLength(1);
    expect(out[0].type).toBe("image/png");
  });

  it("attaches a copied webpage image (html + png, no plain text)", () => {
    const file = pngFile();
    const out = filesFromComposerClipboard({
      items: [
        { kind: "string", type: "text/html" },
        { kind: "file", type: "image/png", getAsFile: () => file },
      ],
      files: [file],
      getData: (type) => (type === "text/html" ? "<img src=\"x\">" : ""),
    });
    expect(out).toHaveLength(1);
  });

  it("does not steal a text selection that also has a screenshot fallback", () => {
    const file = pngFile();
    const out = filesFromComposerClipboard({
      items: [
        { kind: "string", type: "text/plain" },
        { kind: "file", type: "image/png", getAsFile: () => file },
      ],
      files: [file],
      getData: (type) => (type === "text/plain" ? "圈選的段落" : ""),
    });
    expect(out).toEqual([]);
  });

  it("treats a filename-only plain text as an image paste", () => {
    const file = pngFile("image.png");
    const out = filesFromComposerClipboard({
      items: [
        { kind: "string", type: "text/plain" },
        { kind: "file", type: "image/png", getAsFile: () => file },
      ],
      files: [file],
      getData: () => "image.png",
    });
    expect(out).toHaveLength(1);
    expect(out[0].name).toMatch(/^貼上-/);
  });

  it("names a nameless screenshot", () => {
    const raw = new File([new Uint8Array([1])], "image.png", { type: "image/png" });
    const named = namePastedFile(raw);
    expect(named.name).toMatch(/^貼上-.*\.png$/);
  });
});
