import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

import { DEFAULT_FOLDERS } from "../data.jsx";
import {
  mountOrchestrator,
  createFakeBackend,
} from "./helpers/orchestrator.jsx";

const customFolders = [
  ...DEFAULT_FOLDERS,
  { id: "hr", name: "人資專用", icon: "folder" },
];

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("ui-settings 讀取失敗不得回存預設", () => {
  it("GET 503 之後不會自動 PUT 把資料夾與 block 覆寫掉", async () => {
    const backend = createFakeBackend({
      uiSettings: { folders: customFolders, redactionMode: "block" },
    });
    window.localStorage.setItem("anila-folders:1", JSON.stringify(customFolders));
    backend.route(
      "GET",
      "/api/users/me/ui-settings",
      (_req, { errorResponse }) => errorResponse(503, "暫時無法讀取設定"),
    );
    await mountOrchestrator({ backend });
    await new Promise((r) => setTimeout(r, 800));
    const puts = backend.requests.filter(
      (r) => r.path === "/api/users/me/ui-settings" && r.method === "PUT",
    );
    expect(puts).toHaveLength(0);
    expect(JSON.parse(window.localStorage.getItem("anila-folders:1"))).toEqual(customFolders);
  });
});
