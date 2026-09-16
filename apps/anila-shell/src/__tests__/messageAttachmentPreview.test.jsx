import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render } from "@testing-library/react";

import { MessageBubble } from "../chat.jsx";
import {
  attachmentPreviewSrc,
  mapServerAttachments,
  mergeMessageAttachments,
} from "../runtime/messageAttachments.js";

afterEach(cleanup);

function userMsg(extra = {}) {
  return {
    id: "u1",
    role: "user",
    text: "知道這是啥嗎",
    ...extra,
  };
}

describe("attachmentPreviewSrc", () => {
  it("prefers dataUrl, then blob preview, then the download URL", () => {
    expect(attachmentPreviewSrc({
      kind: "image",
      dataUrl: "data:image/png;base64,aa",
      previewUrl: "blob:x",
      referenceId: "r1",
    })).toBe("data:image/png;base64,aa");
    expect(attachmentPreviewSrc({
      kind: "image",
      previewUrl: "blob:x",
      referenceId: "r1",
    })).toBe("blob:x");
    expect(attachmentPreviewSrc({
      kind: "image",
      referenceId: "r1",
      contentType: "image/png",
    })).toBe("/api/attachments/r1");
  });

  it("does not invent a preview for a document", () => {
    expect(attachmentPreviewSrc({
      name: "note.pdf",
      contentType: "application/pdf",
      referenceId: "r2",
    })).toBeNull();
  });
});

describe("mapServerAttachments / merge", () => {
  it("marks image/* as image so the bubble can fetch /api/attachments", () => {
    const mapped = mapServerAttachments([
      {
        reference_id: "img-1",
        filename: "貼上.png",
        content_type: "image/png",
        size_bytes: 12,
      },
    ]);
    expect(mapped[0].kind).toBe("image");
    expect(attachmentPreviewSrc(mapped[0])).toBe("/api/attachments/img-1");
  });

  it("keeps local dataUrl when the server snapshot has no bytes", () => {
    const merged = mergeMessageAttachments(
      [{ id: "img-1", referenceId: "img-1", name: "貼上.png", kind: "image" }],
      [{
        id: "img-1",
        referenceId: "img-1",
        name: "貼上.png",
        kind: "image",
        dataUrl: "data:image/png;base64,aa",
      }],
    );
    expect(merged[0].dataUrl).toBe("data:image/png;base64,aa");
  });
});

describe("sent user bubble shows an image preview", () => {
  it("renders the picture, not a filename pill", () => {
    const { container } = render(
      <MessageBubble
        msg={userMsg({
          attachments: [{
            name: "貼上-2026-09-16.png",
            kind: "image",
            contentType: "image/png",
            dataUrl: "data:image/png;base64,aaaa",
          }],
        })}
        agents={[]}
      />,
    );
    const img = container.querySelector("[data-message-image='1'] img");
    expect(img).not.toBeNull();
    expect(img.getAttribute("src")).toBe("data:image/png;base64,aaaa");
    expect(container.textContent).toContain("知道這是啥嗎");
    expect(container.querySelector("[data-testid='message-att-images']")).not.toBeNull();
  });

  it("keeps documents as a filename chip", () => {
    const { container } = render(
      <MessageBubble
        msg={userMsg({
          attachments: [{
            name: "brief.pdf",
            kind: "file",
            contentType: "application/pdf",
            referenceId: "doc-1",
          }],
        })}
        agents={[]}
      />,
    );
    expect(container.querySelector("[data-message-image]")).toBeNull();
    expect(container.textContent).toContain("brief.pdf");
  });
});
