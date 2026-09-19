import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

import { DEFAULT_FOLDERS } from "../data.jsx";
import {
  mountOrchestrator,
  createFakeBackend,
  screen,
  waitFor,
  fireEvent,
  act,
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

  async function openPrivacy() {
    await act(async () => {
      fireEvent.click(screen.getByTitle("設定"));
    });
    await act(async () => {
      fireEvent.click(screen.getByText("隱私 / 信任").closest("button"));
    });
  }

  function settingsPuts(backend) {
    return backend.requests.filter(
      (r) => r.path === "/api/users/me/ui-settings" && r.method === "PUT",
    );
  }

  it("GET 503 後只改隱私，重試讀取才合併，不會用預設資料夾覆寫", async () => {
    const backend = createFakeBackend({
      uiSettings: { folders: customFolders, redactionMode: "warn" },
    });
    backend.route(
      "GET",
      "/api/users/me/ui-settings",
      (_req, { errorResponse }) => errorResponse(503, "暫時無法讀取設定"),
      { once: true },
    );
    await mountOrchestrator({ backend });
    await openPrivacy();
    expect(screen.getByText(/設定暫時讀不到伺服器/)).toBeTruthy();
    await act(async () => {
      fireEvent.click(screen.getByText("偵測個資時阻止送出"));
    });
    expect(screen.getByText(/這次修改還沒存到伺服器/)).toBeTruthy();
    await new Promise((r) => setTimeout(r, 800));
    expect(settingsPuts(backend)).toHaveLength(0);
    await act(async () => {
      fireEvent.click(screen.getByText("重試"));
    });
    await waitFor(() => {
      const puts = settingsPuts(backend);
      expect(puts.length).toBeGreaterThan(0);
      expect(puts.at(-1).body?.ui_settings?.redactionMode).toBe("block");
      expect(puts.at(-1).body?.ui_settings?.folders).toEqual(customFolders);
    });
  });

  it("PUT 失敗後按重試會再送出修改，不會讀回舊值", async () => {
    const backend = createFakeBackend({
      uiSettings: { folders: customFolders, redactionMode: "warn" },
    });
    backend.route(
      "PUT",
      "/api/users/me/ui-settings",
      (_req, { errorResponse }) => errorResponse(503, "暫時無法寫入設定"),
      { once: true },
    );
    await mountOrchestrator({ backend });
    await openPrivacy();
    await act(async () => {
      fireEvent.click(screen.getByText("偵測個資時阻止送出"));
    });
    await waitFor(() => {
      expect(settingsPuts(backend).length).toBeGreaterThan(0);
      expect(screen.getByText(/這次修改還沒存到伺服器/)).toBeTruthy();
    });
    const getsBefore = backend.requests.filter(
      (r) => r.path === "/api/users/me/ui-settings" && r.method === "GET",
    ).length;
    await act(async () => {
      fireEvent.click(screen.getByText("重試"));
    });
    await waitFor(() => {
      expect(settingsPuts(backend).length).toBeGreaterThan(1);
      expect(settingsPuts(backend).at(-1).body?.ui_settings?.redactionMode).toBe("block");
    });
    const getsAfter = backend.requests.filter(
      (r) => r.path === "/api/users/me/ui-settings" && r.method === "GET",
    ).length;
    expect(getsAfter).toBe(getsBefore);
    expect(screen.getByText("偵測個資時阻止送出").getAttribute("aria-pressed")).toBe("true");
  });
});
