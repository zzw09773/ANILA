import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup, act } from "@testing-library/react";

import { Composer } from "../chat.jsx";
import { ConfirmProvider } from "../confirm.jsx";

const AGENTS = [{ id: "anila-router", name: "ANILA 自動選助手", short: "auto" }];

function renderComposer(props = {}) {
  return render(
    <ConfirmProvider>
      <Composer onSend={vi.fn()} agents={AGENTS} {...props} />
    </ConfirmProvider>,
  );
}

function pngFile(name = "shot.png") {
  return new File([new Uint8Array([137, 80, 78, 71])], name, { type: "image/png" });
}

beforeEach(() => {
  sessionStorage.clear();
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false })));
  if (typeof URL.createObjectURL !== "function") {
    URL.createObjectURL = () => "blob:composer-preview";
  }
  if (typeof URL.revokeObjectURL !== "function") {
    URL.revokeObjectURL = () => {};
  }
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Composer 貼圖", () => {
  it("貼上圖片會變成縮圖附件，不把圖當文字貼進輸入框", async () => {
    const file = pngFile();
    const { container } = renderComposer();
    const ta = screen.getByRole("textbox");

    await act(async () => {
      fireEvent.paste(ta, {
        clipboardData: {
          items: [{ kind: "file", type: "image/png", getAsFile: () => file }],
          files: [file],
          getData: () => "",
        },
      });
    });

    await waitFor(() => {
      const thumb = container.querySelector("[data-composer-image='1']");
      expect(thumb).not.toBeNull();
      expect(thumb.textContent).toContain("shot.png");
    });
    expect(ta.value).toBe("");
  });

  it("圈選文字連同截圖後援時，只貼文字、不附加圖片", async () => {
    const file = pngFile();
    const { container } = renderComposer();
    const ta = screen.getByRole("textbox");

    await act(async () => {
      fireEvent.paste(ta, {
        clipboardData: {
          items: [
            { kind: "string", type: "text/plain" },
            { kind: "file", type: "image/png", getAsFile: () => file },
          ],
          files: [file],
          getData: (type) => (type === "text/plain" ? "圈選的段落" : ""),
        },
      });
    });

    expect(container.querySelector("[data-composer-image='1']")).toBeNull();
  });
});
