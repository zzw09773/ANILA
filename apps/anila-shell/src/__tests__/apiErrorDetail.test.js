import { afterEach, describe, expect, it, vi } from "vitest";
import { authRequest } from "../runtime/api.js";

describe("runtime API error parsing", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("returns the message from an object-shaped detail", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: false,
      status: 403,
      statusText: "Forbidden",
      headers: new Headers({ "content-type": "application/json" }),
      json: async () => ({
        detail: {
          code: "pending_approval",
          message: "等待核准中，請通知 admin",
        },
      }),
    })));

    await expect(authRequest("/api/auth/login", { method: "POST" }))
      .rejects.toMatchObject({
        status: 403,
        message: "等待核准中，請通知 admin",
      });
  });
});
