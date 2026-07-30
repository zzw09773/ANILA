import { describe, expect, it, vi } from "vitest";
import { updateConversation } from "../runtime/conversations.js";

describe("updateConversation meta persistence", () => {
  it("strips classified from tags before PUT and sends star/folder", async () => {
    const authRequest = vi.fn(async () => ({ ok: true }));
    await updateConversation(authRequest, 42, {
      starred: true,
      folder: "usr-x",
      tags: ["urgent", "classified", "hr"],
    });
    expect(authRequest).toHaveBeenCalledTimes(1);
    const [path, opts] = authRequest.mock.calls[0];
    expect(path).toBe("/api/conversations/42");
    expect(opts.method).toBe("PUT");
    expect(JSON.parse(opts.body)).toEqual({
      starred: true,
      folder: "usr-x",
      tags: ["urgent", "hr"],
    });
  });

  it("title-only patch stays compatible with rename", async () => {
    const authRequest = vi.fn(async () => ({ ok: true }));
    await updateConversation(authRequest, 7, { title: "新標題" });
    expect(JSON.parse(authRequest.mock.calls[0][1].body)).toEqual({
      title: "新標題",
    });
  });
});
