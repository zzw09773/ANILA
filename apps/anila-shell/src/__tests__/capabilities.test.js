// 部署能力旗標(W1-3)—— 前端只認白名單旗標,且**失敗時 fail-closed**。
//
// 為什麼 fail-closed 是對的:本包要修的缺陷正是「UI 承諾了部署上不存在的
// 功能」。端點不存在 / 壞掉時,寧可說「未啟用」也不要再次承諾一個永遠不會
// 來的東西。既有殘留資料的檢視與刪除路徑不受此旗標影響(見 memoryTab)。

import { describe, it, expect, vi } from "vitest";

import {
  CAPABILITY_FLAGS,
  DEFAULT_CAPABILITIES,
  fetchCapabilities,
  normalizeCapabilities,
} from "../runtime/capabilities.js";

describe("normalizeCapabilities", () => {
  it("maps the snake_case wire format to camelCase", () => {
    expect(
      normalizeCapabilities({ enable_memory: true, enable_public_share: true }),
    ).toEqual({ enableMemory: true, enablePublicShare: true });
  });

  it("defaults to everything off when the payload is missing", () => {
    expect(normalizeCapabilities(null)).toEqual(DEFAULT_CAPABILITIES);
    expect(normalizeCapabilities(undefined)).toEqual(DEFAULT_CAPABILITIES);
    expect(normalizeCapabilities({})).toEqual(DEFAULT_CAPABILITIES);
  });

  it("coerces non-boolean values instead of leaking them into the UI", () => {
    expect(normalizeCapabilities({ enable_memory: "true" })).toEqual({
      enableMemory: true,
      enablePublicShare: false,
    });
    expect(normalizeCapabilities({ enable_memory: 0 })).toEqual(
      DEFAULT_CAPABILITIES,
    );
  });

  it("drops any key outside the whitelist (no deployment info leak)", () => {
    const out = normalizeCapabilities({
      enable_memory: true,
      DATABASE_URL: "postgresql://secret",
      ANILA_DEPLOYMENT_PROFILE: "formal",
    });
    expect(Object.keys(out).sort()).toEqual(["enableMemory", "enablePublicShare"]);
  });

  it("exposes exactly the two flags this wave needs", () => {
    expect([...CAPABILITY_FLAGS].sort()).toEqual([
      "enable_memory",
      "enable_public_share",
    ]);
  });
});

describe("fetchCapabilities", () => {
  it("reads GET /api/capabilities", async () => {
    const request = vi.fn().mockResolvedValueOnce({
      enable_memory: true,
      enable_public_share: false,
    });
    const caps = await fetchCapabilities(request);
    expect(request).toHaveBeenCalledWith("/api/capabilities");
    expect(caps).toEqual({ enableMemory: true, enablePublicShare: false });
  });

  it("fails closed when the endpoint is absent (older backend)", async () => {
    const request = vi
      .fn()
      .mockRejectedValueOnce(Object.assign(new Error("not found"), { status: 404 }));
    await expect(fetchCapabilities(request)).resolves.toEqual(DEFAULT_CAPABILITIES);
  });

  it("fails closed on any transport error", async () => {
    const request = vi.fn().mockRejectedValueOnce(new Error("offline"));
    await expect(fetchCapabilities(request)).resolves.toEqual(DEFAULT_CAPABILITIES);
  });
});
