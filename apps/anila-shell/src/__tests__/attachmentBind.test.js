// 上傳／bind 控制面 wrapper：conversation_id 必須真的進 FormData / JSON。

import { describe, it, expect } from "vitest";

import { bindAttachments, uploadAttachment } from "../runtime/conversations.js";

describe("uploadAttachment", () => {
  it("數字 conversationId 會寫進 FormData", async () => {
    const calls = [];
    const multipartRequest = async (path, form) => {
      calls.push({ path, form });
      return { reference_id: "r1" };
    };
    const file = new File(["hi"], "note.md", { type: "text/markdown" });
    await uploadAttachment(multipartRequest, file, { conversationId: 9 });
    expect(calls[0].path).toBe("/api/attachments");
    expect(calls[0].form.get("conversation_id")).toBe("9");
  });

  it("沒有數字 conversationId 時不帶 conversation_id（避免 orphan 被寫成 NaN）", async () => {
    const calls = [];
    const multipartRequest = async (path, form) => {
      calls.push({ path, form });
      return { reference_id: "r1" };
    };
    const file = new File(["hi"], "note.md", { type: "text/markdown" });
    await uploadAttachment(multipartRequest, file, { conversationId: "cv-local" });
    expect(calls[0].form.get("conversation_id")).toBeNull();
  });
});

describe("bindAttachments", () => {
  it("POST /api/attachments/bind 帶 conversation_id 與 reference_ids", async () => {
    const calls = [];
    const authRequest = async (path, options) => {
      calls.push({ path, options });
      return { conversation_id: 3, attachments: [] };
    };
    await bindAttachments(authRequest, {
      conversationId: 3,
      referenceIds: ["abc", "def"],
    });
    expect(calls[0].path).toBe("/api/attachments/bind");
    expect(calls[0].options.method).toBe("POST");
    expect(JSON.parse(calls[0].options.body)).toEqual({
      conversation_id: 3,
      reference_ids: ["abc", "def"],
    });
  });
});
