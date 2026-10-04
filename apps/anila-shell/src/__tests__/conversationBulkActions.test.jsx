// 側欄對話多選：刪除、移入同一個可寫群組。
// 這些斷言看的是畫面與假後端請求，不是 helper 自己的回傳值。
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { Sidebar } from "../chat.jsx";
import { ConfirmProvider } from "../confirm.jsx";
import { DEFAULT_FOLDERS } from "../data.jsx";
import { headerValue } from "./helpers/fakeBackend.js";
import {
  TEST_CSRF_TOKEN,
  act,
  composerBox,
  createFakeBackend,
  mountOrchestrator,
  selectConversation,
  sendText,
  waitForIdle,
} from "./helpers/orchestrator.jsx";

const FOLDERS = [
  { id: "all", name: "全部", icon: "inbox" },
  { id: "starred", name: "已加星", icon: "star" },
  { id: "proj", name: "專案", icon: "folder" },
];

function convRow(id, title, extra = {}) {
  return {
    id,
    title,
    agent_id: null,
    classified: false,
    tags: [],
    starred: false,
    folder: "all",
    updated_at: "2026-10-01T00:00:00Z",
    created_at: "2026-10-01T00:00:00Z",
    ...extra,
  };
}

function seedPair(backend) {
  backend.seedMessages(55, [
    { id: 550, role: "user", content: "甲的問題" },
    { id: 551, role: "assistant", content: "甲的回答" },
  ]);
  backend.seedMessages(66, [
    { id: 660, role: "user", content: "乙的問題" },
    { id: 661, role: "assistant", content: "乙的回答" },
  ]);
}

async function enterBulk() {
  fireEvent.click(screen.getByRole("button", { name: "多選" }));
  return screen.findByTestId("conv-bulk-bar");
}

function reactProps(element) {
  const key = Object.keys(element).find((name) => name.startsWith("__reactProps"));
  return key ? element[key] : null;
}

async function invokeOnClick(element) {
  const onClick = reactProps(element)?.onClick;
  if (typeof onClick !== "function") throw new Error("找不到 onClick");
  await act(async () => {
    onClick({ preventDefault() {}, stopPropagation() {} });
  });
}

async function openBulkFromDisabledEntry() {
  const entry = screen.getByRole("button", { name: "多選" });
  expect(entry).toBeDisabled();
  await invokeOnClick(entry);
  return screen.findByTestId("conv-bulk-bar");
}

function convBox(title) {
  return screen.getByRole("checkbox", { name: `選取對話 ${title}` });
}

// 側欄列標題是按鈕裡的 div；頂端標題是 span，兩者 title 相同。
function sidebarTitles(title) {
  return screen.queryAllByTitle(title).filter((node) => (
    node.tagName === "DIV" && node.closest("button")
  ));
}

async function confirmDialog(dialogName, buttonName) {
  const dialog = await screen.findByRole("dialog", { name: dialogName });
  fireEvent.click(within(dialog).getByRole("button", { name: buttonName }));
  return dialog;
}

function expectBatchIdentity(record, id = "1") {
  expect(headerValue(record, "X-ANILA-Expected-User-ID")).toBe(String(id));
  expect(headerValue(record, "X-CSRF-Token")).toBe(TEST_CSRF_TOKEN);
}

function conversationWrites(backend) {
  return backend.requests.filter((record) => (
    (record.method === "PUT" || record.method === "PATCH" || record.method === "DELETE")
    && /^\/api\/conversations\/[^/]+$/.test(record.path)
  ));
}

function userMessageIncludes(text) {
  return [...document.querySelectorAll(".anila-msg-user")].some((node) => (
    node.textContent.includes(text)
  ));
}

async function mountMissingUserMixed() {
  const backend = createFakeBackend({
    user: { username: "tester", display_name: "測試使用者" },
    uiSettings: { folders: FOLDERS },
    conversations: [convRow(55, "對話甲")],
  });
  backend.seedMessages(55, [
    { id: 550, role: "user", content: "甲的問題" },
    { id: 551, role: "assistant", content: "甲的回答" },
  ]);
  backend.disableTitleGeneration().enqueueManualStream();
  backend.route("POST", "/api/conversations", (_req, { errorResponse }) => errorResponse(500, "離線"));
  await mountOrchestrator({ backend });
  await sendText("離線這則要留著");
  expect(await screen.findByLabelText("停止產生")).toBeInTheDocument();
  backend.stream?.close();
  await waitForIdle();
  expect(sidebarTitles("離線這則要留著")).toHaveLength(1);
  expect(userMessageIncludes("離線這則要留著")).toBe(true);
  expect(await screen.findByRole("button", { name: "專案" })).toBeInTheDocument();
  await selectConversation("對話甲");
  expect(await screen.findByText("甲的回答")).toBeInTheDocument();
  return {
    backend,
    writesBefore: conversationWrites(backend).length,
    serverMessages: backend.storedMessages(55).map((row) => row.content),
  };
}

async function finishConfirmIfOpened(dialogName, buttonName) {
  const dialog = screen.queryByRole("dialog", { name: dialogName });
  const opened = Boolean(dialog);
  if (dialog) {
    fireEvent.click(within(dialog).getByRole("button", { name: buttonName }));
    await waitFor(() => {
      expect(screen.queryByTestId("conv-bulk-status")).toBeTruthy();
    });
  } else {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 30));
    });
  }
  return opened;
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("側欄對話多選", () => {
  it("一次確認後刪除多則，清掉成功項的訊息與作用中對話，其他對話留著", async () => {
    const backend = createFakeBackend({
      conversations: [convRow(55, "對話甲"), convRow(66, "對話乙"), convRow(77, "對話丙")],
    });
    seedPair(backend);
    backend.seedMessages(77, [
      { id: 770, role: "user", content: "丙的問題" },
      { id: 771, role: "assistant", content: "丙的回答" },
    ]);
    await mountOrchestrator({ backend });

    await selectConversation("對話丙");
    expect(await screen.findByText("丙的回答")).toBeInTheDocument();
    const getsBefore = backend.requestsFor(/^\/api\/conversations\/77$/, "GET").length;
    expect(getsBefore).toBeGreaterThan(0);

    const bar = await enterBulk();
    fireEvent.click(convBox("對話甲"));
    fireEvent.click(convBox("對話乙"));
    expect(within(bar).getByTestId("conv-bulk-count").textContent).toMatch(/已選 2/);
    fireEvent.click(within(bar).getByRole("button", { name: "刪除" }));
    const dialog = await screen.findByRole("dialog", { name: "刪除對話" });
    expect(dialog.textContent).toMatch(/2 則對話/);
    expect(dialog.textContent).toMatch(/無法復原/);
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "刪除對話" })).not.toBeInTheDocument();
      expect(within(screen.getByTestId("conv-bulk-bar")).getByRole("button", { name: "刪除" })).toBeEnabled();
    });
    expect(backend.requestsFor(/^\/api\/conversations\/55$/, "DELETE")).toHaveLength(0);
    expect(screen.getByText("丙的回答")).toBeInTheDocument();

    fireEvent.click(within(screen.getByTestId("conv-bulk-bar")).getByRole("button", { name: "刪除" }));
    await confirmDialog("刪除對話", "刪除");

    await waitFor(() => {
      expect(screen.queryByText("對話甲")).toBeNull();
      expect(screen.queryByText("對話乙")).toBeNull();
    });
    expect(screen.getByText("丙的回答")).toBeInTheDocument();
    expect(screen.getAllByText("對話丙").length).toBeGreaterThanOrEqual(2);
    expect(backend.requestsFor(/^\/api\/conversations\/55$/, "DELETE")).toHaveLength(1);
    expect(backend.requestsFor(/^\/api\/conversations\/66$/, "DELETE")).toHaveLength(1);
    expectBatchIdentity(backend.requestsFor(/^\/api\/conversations\/55$/, "DELETE")[0]);
    expectBatchIdentity(backend.requestsFor(/^\/api\/conversations\/66$/, "DELETE")[0]);
    expect(backend.requestsFor(/^\/api\/conversations\/77$/, "DELETE")).toHaveLength(0);
    expect(backend.requestsFor(/^\/api\/conversations\/77$/, "GET").length).toBe(getsBefore);
    expect(backend.conversationIds()).not.toContain(55);
    expect(backend.conversationIds()).not.toContain(66);
    expect(backend.conversationIds()).toContain(77);
  });

  it("部分刪除失敗時只拿掉成功項，失敗的對話、訊息與選取都留著", async () => {
    const backend = createFakeBackend({
      conversations: [convRow(55, "對話甲"), convRow(66, "對話乙")],
    });
    seedPair(backend);
    backend.route("DELETE", /^\/api\/conversations\/66$/, (_req, { errorResponse }) =>
      errorResponse(500, "刪除失敗"),
    );
    await mountOrchestrator({ backend });
    await selectConversation("對話乙");
    expect(await screen.findByText("乙的回答")).toBeInTheDocument();

    const bar = await enterBulk();
    fireEvent.click(convBox("對話甲"));
    fireEvent.click(convBox("對話乙"));
    fireEvent.click(within(bar).getByRole("button", { name: "刪除" }));
    await confirmDialog("刪除對話", "刪除");

    await waitFor(() => {
      expect(screen.queryByText("對話甲")).toBeNull();
    });
    expect(screen.getAllByText("對話乙").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("乙的回答")).toBeInTheDocument();
    expect(screen.getByTestId("conv-bulk-status").textContent).toMatch(/已刪除 1 則/);
    expect(screen.getByTestId("conv-bulk-status").textContent).toMatch(/1 則失敗/);
    expect(screen.getByTestId("conv-bulk-status").textContent).toMatch(/仍保留/);
    expect(screen.getByTestId("conv-bulk-status").textContent).not.toMatch(/已刪除 2 則/);
    expect(convBox("對話乙").checked).toBe(true);
    expect(backend.conversationIds()).not.toContain(55);
    expect(backend.conversationIds()).toContain(66);
    expect(backend.storedMessages(66).map((m) => m.content)).toContain("乙的回答");
  });

  it("搜尋補進來、不在本地清單的對話可以選取並刪除", async () => {
    const backend = createFakeBackend({
      conversations: [convRow(55, "對話甲")],
    });
    backend.route("GET", "/api/conversations/search", (_req, { jsonResponse }) =>
      jsonResponse([
        {
          id: 999,
          title: "遠端舊對話",
          agent_id: null,
          updated_at: "2026-10-02T00:00:00Z",
          snippet: "只有伺服器找得到",
        },
      ]),
    );
    await mountOrchestrator({ backend });
    expect(backend.conversationIds()).not.toContain(999);

    fireEvent.change(screen.getByRole("textbox", { name: "搜尋對話" }), {
      target: { value: "遠端舊對話" },
    });
    expect(await screen.findByText("遠端舊對話")).toBeInTheDocument();
    const bar = await enterBulk();
    const box = convBox("遠端舊對話");
    expect(box.tagName).toBe("INPUT");
    expect(box).toHaveProperty("type", "checkbox");
    fireEvent.click(box);
    expect(within(bar).getByTestId("conv-bulk-count").textContent).toMatch(/已選 1/);
    fireEvent.click(within(bar).getByRole("button", { name: "刪除" }));
    const dialog = await screen.findByRole("dialog", { name: "刪除對話" });
    expect(dialog.textContent).toMatch(/1 則對話/);
    expect(dialog.textContent).toMatch(/無法復原/);
    fireEvent.click(within(dialog).getByRole("button", { name: "刪除" }));

    await waitFor(() => {
      expect(screen.queryByText("遠端舊對話")).toBeNull();
    });
    expect(backend.requestsFor(/^\/api\/conversations\/999$/, "DELETE")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "清除搜尋" }));
    expect(screen.getByText("對話甲")).toBeInTheDocument();
    expect(backend.conversationIds()).toContain(55);
  });

  it("遠端命中可移入自訂群組；選項沒有已加星，取消群組會送 folder all", async () => {
    const backend = createFakeBackend({
      uiSettings: { folders: FOLDERS },
      conversations: [convRow(55, "對話甲", { folder: "proj" })],
    });
    backend.route("GET", "/api/conversations/search", (_req, { jsonResponse }) =>
      jsonResponse([
        {
          id: 999,
          title: "遠端舊對話",
          updated_at: "2026-10-02T00:00:00Z",
        },
      ]),
    );
    await mountOrchestrator({ backend });
    expect(await screen.findByRole("button", { name: "專案" })).toBeInTheDocument();

    fireEvent.change(screen.getByRole("textbox", { name: "搜尋對話" }), {
      target: { value: "遠端舊對話" },
    });
    expect(await screen.findByText("遠端舊對話")).toBeInTheDocument();
    const bar = await enterBulk();
    const select = within(bar).getByLabelText("移到群組");
    const labels = [...select.options].map((o) => o.textContent);
    const values = [...select.options].map((o) => o.value);
    expect(labels).toContain("專案");
    expect(labels).toContain("取消群組");
    expect(labels).not.toContain("已加星");
    expect(values).not.toContain("starred");
    fireEvent.click(convBox("遠端舊對話"));
    fireEvent.change(select, { target: { value: "proj" } });
    fireEvent.click(within(bar).getByRole("button", { name: "移入" }));
    const dialog = await screen.findByRole("dialog", { name: "移入群組" });
    expect(dialog.textContent).toMatch(/1 則對話/);
    expect(dialog.textContent).toMatch(/專案/);
    fireEvent.click(within(dialog).getByRole("button", { name: "移入" }));

    await waitFor(() => {
      expect(backend.requestsFor(/^\/api\/conversations\/999$/, "PUT").length).toBeGreaterThan(0);
    });
    const put = backend.requestsFor(/^\/api\/conversations\/999$/, "PUT").at(-1);
    expect(put.body).toMatchObject({ folder: "proj" });
    expect(put.body.starred).toBeUndefined();
    expectBatchIdentity(put);

    fireEvent.click(screen.getByRole("button", { name: "清除搜尋" }));
    fireEvent.click(screen.getByRole("button", { name: "專案" }));
    expect(await screen.findByText("遠端舊對話")).toBeInTheDocument();

    const bar2 = screen.getByTestId("conv-bulk-bar");
    fireEvent.click(convBox("遠端舊對話"));
    fireEvent.change(within(bar2).getByLabelText("移到群組"), { target: { value: "all" } });
    fireEvent.click(within(bar2).getByRole("button", { name: "移入" }));
    const clearDialog = await screen.findByRole("dialog", { name: "移入群組" });
    expect(clearDialog.textContent).toMatch(/取消群組/);
    fireEvent.click(within(clearDialog).getByRole("button", { name: "移入" }));
    await waitFor(() => {
      const last = backend.requestsFor(/^\/api\/conversations\/999$/, "PUT").at(-1);
      expect(last.body).toMatchObject({ folder: "all" });
    });
    await waitFor(() => {
      expect(screen.queryByText("遠端舊對話")).toBeNull();
    });
  });

  it("部分移入失敗時，成功的改群組、失敗的留在原群組", async () => {
    const backend = createFakeBackend({
      uiSettings: { folders: FOLDERS },
      conversations: [convRow(55, "對話甲"), convRow(66, "對話乙")],
    });
    backend.route("PUT", /^\/api\/conversations\/66$/, (_req, { errorResponse }) =>
      errorResponse(500, "移入失敗"),
    );
    await mountOrchestrator({ backend });
    expect(await screen.findByRole("button", { name: "專案" })).toBeInTheDocument();

    const bar = await enterBulk();
    fireEvent.click(convBox("對話甲"));
    fireEvent.click(convBox("對話乙"));
    fireEvent.change(within(bar).getByLabelText("移到群組"), { target: { value: "proj" } });
    fireEvent.click(within(bar).getByRole("button", { name: "移入" }));
    await confirmDialog("移入群組", "移入");

    await waitFor(() => {
      expect(screen.getByTestId("conv-bulk-status").textContent).toMatch(/1 則失敗/);
    });
    expect(screen.getByTestId("conv-bulk-status").textContent).toMatch(/仍留在原處/);
    expect(backend.storedConversation(55).folder).toBe("proj");
    expect(backend.storedConversation(66).folder).not.toBe("proj");
    expect(backend.requestsFor(/^\/api\/conversations\/55$/, "PUT").some((r) => r.body?.folder === "proj")).toBe(true);
    expect(convBox("對話乙").checked).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "專案" }));
    await waitFor(() => {
      expect(screen.getByText("對話甲")).toBeInTheDocument();
    });
    expect(screen.queryByText("對話乙")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "全部" }));
    expect(await screen.findByText("對話乙")).toBeInTheDocument();
    expect(backend.storedConversation(66).folder).not.toBe("proj");
  });

  it("搜尋或切換群組會清掉選取，不會刪到已經看不見的對話", async () => {
    const backend = createFakeBackend({
      uiSettings: { folders: FOLDERS },
      conversations: [
        convRow(55, "對話甲"),
        convRow(66, "對話乙", { folder: "proj" }),
      ],
    });
    await mountOrchestrator({ backend });
    expect(await screen.findByRole("button", { name: "專案" })).toBeInTheDocument();

    const bar = await enterBulk();
    fireEvent.click(convBox("對話甲"));
    fireEvent.click(convBox("對話乙"));
    expect(within(bar).getByTestId("conv-bulk-count").textContent).toMatch(/已選 2/);

    fireEvent.change(screen.getByRole("textbox", { name: "搜尋對話" }), {
      target: { value: "不會出現的字" },
    });
    await waitFor(() => {
      expect(screen.getByTestId("conv-bulk-count").textContent).toMatch(/已選 0/);
    });
    expect(screen.queryByRole("checkbox", { name: "選取對話 對話甲" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "清除搜尋" }));
    expect(convBox("對話甲").checked).toBe(false);
    expect(convBox("對話乙").checked).toBe(false);

    fireEvent.click(convBox("對話甲"));
    fireEvent.click(screen.getByRole("button", { name: "專案" }));
    await waitFor(() => {
      expect(screen.getByTestId("conv-bulk-count").textContent).toMatch(/已選 0/);
    });
    expect(screen.queryByText("對話甲")).toBeNull();
    expect(within(screen.getByTestId("conv-bulk-bar")).getByRole("button", { name: "刪除" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "全部" }));
    expect(screen.getByText("對話甲")).toBeInTheDocument();
    expect(backend.requestsFor(/^\/api\/conversations\/55$/, "DELETE")).toHaveLength(0);
    expect(backend.requestsFor(/^\/api\/conversations\/66$/, "DELETE")).toHaveLength(0);
  });

  it("串流中的對話不能選，全選與批次刪除都會跳過", async () => {
    const backend = createFakeBackend({
      conversations: [convRow(55, "對話甲")],
    });
    backend.disableTitleGeneration();
    backend.enqueueManualStream();
    await mountOrchestrator({ backend });
    const { sendText } = await import("./helpers/orchestrator.jsx");
    await sendText("慢慢回答");
    expect(await screen.findByLabelText("停止產生")).toBeInTheDocument();

    const bar = await enterBulk();
    const streaming = convBox("慢慢回答");
    expect(streaming).toBeDisabled();
    expect(screen.getByText("請等待完成")).toBeInTheDocument();
    fireEvent.click(streaming);
    expect(streaming.checked).toBe(false);

    fireEvent.click(within(bar).getByRole("button", { name: "全選" }));
    expect(convBox("對話甲").checked).toBe(true);
    expect(streaming.checked).toBe(false);
    expect(within(bar).getByTestId("conv-bulk-count").textContent).toMatch(/已選 1/);

    fireEvent.click(within(bar).getByRole("button", { name: "刪除" }));
    const dialog = await screen.findByRole("dialog", { name: "刪除對話" });
    expect(dialog.textContent).toMatch(/1 則對話/);
    expect(dialog.textContent).not.toMatch(/2 則/);
    fireEvent.click(within(dialog).getByRole("button", { name: "刪除" }));

    await waitFor(() => {
      expect(screen.queryByText("對話甲")).toBeNull();
    });
    expect(screen.getAllByText("慢慢回答").length).toBeGreaterThan(0);
    expect(screen.getByLabelText("停止產生")).toBeInTheDocument();
    const streamingDeletes = backend.requests.filter(
      (r) => r.method === "DELETE" && /^\/api\/conversations\/\d+$/.test(r.path) && r.path !== "/api/conversations/55",
    );
    expect(streamingDeletes).toHaveLength(0);
    expect(backend.requestsFor(/^\/api\/conversations\/55$/, "DELETE")).toHaveLength(1);
    backend.stream?.close();
  });

  it("批次刪除同時最多 4 筆，五則最後都會刪掉", async () => {
    const backend = createFakeBackend({
      conversations: [1, 2, 3, 4, 5].map((id) => convRow(id, `對話${id}`)),
    });
    let inflight = 0;
    let maxInflight = 0;
    const release = [];
    backend.route("DELETE", /^\/api\/conversations\/\d+$/, async () => {
      inflight += 1;
      maxInflight = Math.max(maxInflight, inflight);
      await new Promise((resolve) => release.push(resolve));
      inflight -= 1;
      return undefined;
    });
    await mountOrchestrator({ backend });
    const bar = await enterBulk();
    fireEvent.click(within(bar).getByRole("button", { name: "全選" }));
    expect(within(bar).getByTestId("conv-bulk-count").textContent).toMatch(/已選 5/);
    fireEvent.click(within(bar).getByRole("button", { name: "刪除" }));
    await confirmDialog("刪除對話", "刪除");

    await waitFor(() => {
      expect(release.length).toBeGreaterThan(0);
    });
    expect(maxInflight).toBeLessThanOrEqual(4);

    const deadline = Date.now() + 5000;
    while (screen.queryByText(/已刪除 5 則/) == null) {
      if (Date.now() > deadline) {
        throw new Error(`批次刪除沒有完成 max=${maxInflight} inflight=${inflight} waiting=${release.length}`);
      }
      const batch = release.splice(0);
      expect(batch.length).toBeLessThanOrEqual(4);
      await act(async () => {
        batch.forEach((fn) => fn());
      });
      await new Promise((r) => setTimeout(r, 20));
    }
    expect(maxInflight).toBeLessThanOrEqual(4);
    expect(maxInflight).toBeGreaterThan(0);
    for (const id of [1, 2, 3, 4, 5]) {
      expect(screen.queryByText(`對話${id}`)).toBeNull();
      expect(backend.conversationIds()).not.toContain(id);
    }
  });

  it("單筆刪除仍走原本的確認，不改成批次", async () => {
    const backend = createFakeBackend({
      conversations: [convRow(55, "對話甲"), convRow(66, "對話乙")],
    });
    await mountOrchestrator({ backend });
    const more = screen.getAllByTitle("更多");
    fireEvent.click(more[0]);
    fireEvent.click(await screen.findByText("刪除對話"));
    const dialog = await screen.findByRole("dialog", { name: "刪除對話" });
    expect(dialog.textContent).toMatch(/對話甲|對話乙/);
    expect(dialog.textContent).toMatch(/無法復原/);
    expect(dialog.textContent).not.toMatch(/2 則/);
    fireEvent.click(within(dialog).getByRole("button", { name: "刪除" }));
    await waitFor(() => {
      const deletes = backend.requests.filter(
        (r) => r.method === "DELETE" && /^\/api\/conversations\/\d+$/.test(r.path),
      );
      expect(deletes).toHaveLength(1);
      expect(headerValue(deletes[0], "X-CSRF-Token")).toBe(TEST_CSRF_TOKEN);
      expect(headerValue(deletes[0], "X-ANILA-Expected-User-ID")).toBeUndefined();
    });
  });

  it("確認期間才開始串流的對話，送出刪除前會跳過並說明等待完成", async () => {
    const backend = createFakeBackend({
      conversations: [convRow(55, "對話甲"), convRow(66, "對話乙")],
    });
    backend.disableTitleGeneration();
    await mountOrchestrator({ backend });
    await selectConversation("對話甲");
    const bar = await enterBulk();
    fireEvent.click(convBox("對話甲"));
    fireEvent.click(convBox("對話乙"));
    fireEvent.click(within(bar).getByRole("button", { name: "刪除" }));
    const dialog = await screen.findByRole("dialog", { name: "刪除對話" });
    expect(screen.getByRole("button", { name: /新對話/ })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "搜尋對話" })).toBeEnabled();
    expect(composerBox()).toBeEnabled();
    expect(screen.getAllByTitle("更多")[0]).toBeDisabled();
    backend.enqueueManualStream();
    await sendText("慢慢回答");
    expect(await screen.findByLabelText("停止產生")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "刪除" }));
    await waitFor(() => {
      expect(screen.queryByText("對話乙")).toBeNull();
    });
    expect(screen.getAllByText("對話甲").length).toBeGreaterThan(0);
    expect(screen.getByLabelText("停止產生")).toBeInTheDocument();
    expect(backend.requestsFor(/^\/api\/conversations\/55$/, "DELETE")).toHaveLength(0);
    expect(backend.requestsFor(/^\/api\/conversations\/66$/, "DELETE")).toHaveLength(1);
    expect(screen.getByTestId("conv-bulk-status").textContent).toMatch(/等待完成/);
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /新對話/ })).toBeEnabled();
    });
    backend.stream?.close();
  });

  it("排隊等刪除空位時才開始串流的對話不會被刪", async () => {
    const backend = createFakeBackend({
      conversations: [1, 2, 3, 4, 5].map((id) => convRow(id, `對話${id}`, {
        updated_at: `2026-10-0${6 - id}T00:00:00Z`,
      })),
    });
    backend.disableTitleGeneration();
    const release = [];
    const seen = [];
    backend.route("DELETE", /^\/api\/conversations\/\d+$/, async (req) => {
      seen.push(Number(req.path.split("/").pop()));
      await new Promise((resolve) => release.push(resolve));
      return undefined;
    });
    await mountOrchestrator({ backend });
    await selectConversation("對話5");
    const bar = await enterBulk();
    fireEvent.click(within(bar).getByRole("button", { name: "全選" }));
    fireEvent.click(within(bar).getByRole("button", { name: "刪除" }));
    await confirmDialog("刪除對話", "刪除");
    await waitFor(() => {
      expect(seen).toHaveLength(4);
    });
    expect(seen).not.toContain(5);
    expect(composerBox()).toBeEnabled();
    backend.enqueueManualStream();
    await sendText("慢慢回答");
    expect(await screen.findByLabelText("停止產生")).toBeInTheDocument();
    expect(backend.requestsFor(/^\/api\/conversations\/5$/, "DELETE")).toHaveLength(0);
    const deadline = Date.now() + 5000;
    while (screen.queryByTestId("conv-bulk-status")?.textContent?.includes("等待完成") !== true) {
      if (Date.now() > deadline) {
        throw new Error(`排隊中的串流對話仍被刪 seen=${seen.join(",")}`);
      }
      const batch = release.splice(0);
      await act(async () => {
        batch.forEach((fn) => fn());
      });
      await new Promise((r) => setTimeout(r, 20));
    }
    expect(backend.requestsFor(/^\/api\/conversations\/5$/, "DELETE")).toHaveLength(0);
    expect(sidebarTitles("對話5")).toHaveLength(1);
    for (const id of [1, 2, 3, 4]) {
      expect(sidebarTitles(`對話${id}`)).toHaveLength(0);
    }
    backend.stream?.close();
  });

  it("離線對話批次刪除與移入只改本地，伺服器數字 id 仍打原端點", async () => {
    const backend = createFakeBackend({
      uiSettings: { folders: FOLDERS },
      conversations: [convRow(55, "對話甲")],
    });
    backend.disableTitleGeneration().enqueueManualStream();
    backend.route("POST", "/api/conversations", (_req, { errorResponse }) => errorResponse(500, "離線"));
    await mountOrchestrator({ backend });
    await sendText("離線草稿");
    expect(await screen.findByLabelText("停止產生")).toBeInTheDocument();
    backend.stream?.close();
    await waitForIdle();
    expect(sidebarTitles("離線草稿")).toHaveLength(1);
    expect(await screen.findByRole("button", { name: "專案" })).toBeInTheDocument();

    const moveBar = await enterBulk();
    fireEvent.click(convBox("離線草稿"));
    fireEvent.click(convBox("對話甲"));
    fireEvent.change(within(moveBar).getByLabelText("移到群組"), { target: { value: "proj" } });
    fireEvent.click(within(moveBar).getByRole("button", { name: "移入" }));
    await confirmDialog("移入群組", "移入");
    await waitFor(() => {
      expect(screen.getByTestId("conv-bulk-status").textContent).toMatch(/已移入 2 則/);
    });
    const puts = backend.requests.filter((r) => r.method === "PUT" && r.body?.folder === "proj");
    expect(puts.map((r) => r.path)).toEqual(["/api/conversations/55"]);
    expectBatchIdentity(puts[0]);
    expect(backend.requests.some((r) => r.method === "PUT" && /cv-local/.test(r.path))).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "專案" }));
    await waitFor(() => {
      expect(sidebarTitles("離線草稿")).toHaveLength(1);
      expect(sidebarTitles("對話甲")).toHaveLength(1);
    });

    fireEvent.click(screen.getByRole("button", { name: "全部" }));
    const deleteBar = screen.getByTestId("conv-bulk-bar");
    fireEvent.click(convBox("離線草稿"));
    fireEvent.click(convBox("對話甲"));
    fireEvent.click(within(deleteBar).getByRole("button", { name: "刪除" }));
    await confirmDialog("刪除對話", "刪除");
    await waitFor(() => {
      expect(sidebarTitles("離線草稿")).toHaveLength(0);
      expect(sidebarTitles("對話甲")).toHaveLength(0);
    });
    const deletes = backend.requests.filter(
      (r) => r.method === "DELETE" && r.path.startsWith("/api/conversations/"),
    );
    expect(deletes.map((r) => r.path)).toEqual(["/api/conversations/55"]);
    expectBatchIdentity(deletes[0]);
  });

  it("單筆刪除失敗只放回那一則，不會清掉期間新建立的對話", async () => {
    const backend = createFakeBackend({
      conversations: [
        convRow(55, "對話甲"),
        convRow(66, "對話乙", { updated_at: "2026-10-03T00:00:00Z" }),
      ],
    });
    backend.disableTitleGeneration();
    let failDelete;
    const gate = new Promise((resolve) => {
      failDelete = resolve;
    });
    backend.route("DELETE", /^\/api\/conversations\/66$/, async (_req, { errorResponse }) => {
      await gate;
      return errorResponse(500, "刪除失敗");
    });
    await mountOrchestrator({ backend });
    const card = screen.getByTitle("對話乙").closest("button").parentElement;
    fireEvent.click(within(card).getByTitle("更多"));
    fireEvent.click(await screen.findByText("刪除對話"));
    await confirmDialog("刪除對話", "刪除");
    await waitFor(() => {
      expect(backend.requestsFor(/^\/api\/conversations\/66$/, "DELETE")).toHaveLength(1);
      expect(screen.queryByTitle("對話乙")).toBeNull();
    });
    await sendText("臨時新話題");
    await waitFor(() => {
      expect(sidebarTitles("臨時新話題")).toHaveLength(1);
    });
    failDelete();
    await waitFor(() => {
      expect(sidebarTitles("對話乙")).toHaveLength(1);
    });
    expect(sidebarTitles("臨時新話題")).toHaveLength(1);
    expect(sidebarTitles("對話甲")).toHaveLength(1);
  });

  it("批次刪除與移入的 409 留著失敗對話與勾選，並顯示登入帳號已變更", async () => {
    const backend = createFakeBackend({
      uiSettings: { folders: FOLDERS },
      conversations: [convRow(55, "對話甲"), convRow(66, "對話乙")],
    });
    const accountChanged = "登入帳號已變更，請重新整理後再操作";
    backend.route("DELETE", /^\/api\/conversations\/66$/, (_req, { errorResponse }) =>
      errorResponse(409, accountChanged),
    );
    backend.route("PUT", /^\/api\/conversations\/66$/, (_req, { errorResponse }) =>
      errorResponse(409, accountChanged),
    );
    await mountOrchestrator({ backend });
    expect(await screen.findByRole("button", { name: "專案" })).toBeInTheDocument();

    const bar = await enterBulk();
    fireEvent.click(convBox("對話甲"));
    fireEvent.click(convBox("對話乙"));
    fireEvent.click(within(bar).getByRole("button", { name: "刪除" }));
    await confirmDialog("刪除對話", "刪除");

    await waitFor(() => {
      expect(screen.getByTestId("conv-bulk-status").textContent).toMatch(/登入帳號已變更/);
    });
    const deleteStatus = screen.getByTestId("conv-bulk-status").textContent;
    expect(deleteStatus).toMatch(/已刪除 1 則/);
    expect(deleteStatus).toMatch(/1 則失敗/);
    expect(deleteStatus).toMatch(/仍保留/);
    expect(deleteStatus).not.toMatch(/已刪除 2 則/);
    expect(screen.queryByText("對話甲")).toBeNull();
    expect(screen.getAllByText("對話乙").length).toBeGreaterThan(0);
    expect(convBox("對話乙").checked).toBe(true);
    const failedDelete = backend.requestsFor(/^\/api\/conversations\/66$/, "DELETE")[0];
    expectBatchIdentity(failedDelete);
    expectBatchIdentity(backend.requestsFor(/^\/api\/conversations\/55$/, "DELETE")[0]);
    expect(backend.conversationIds()).not.toContain(55);
    expect(backend.conversationIds()).toContain(66);

    const moveBar = screen.getByTestId("conv-bulk-bar");
    if (!convBox("對話乙").checked) fireEvent.click(convBox("對話乙"));
    fireEvent.change(within(moveBar).getByLabelText("移到群組"), { target: { value: "proj" } });
    fireEvent.click(within(moveBar).getByRole("button", { name: "移入" }));
    await confirmDialog("移入群組", "移入");
    await waitFor(() => {
      expect(screen.getByTestId("conv-bulk-status").textContent).toMatch(/登入帳號已變更/);
    });
    const moveStatus = screen.getByTestId("conv-bulk-status").textContent;
    expect(moveStatus).toMatch(/1 則失敗/);
    expect(moveStatus).toMatch(/仍留在原處/);
    expect(moveStatus).not.toMatch(/已移入 1 則對話。/);
    expect(convBox("對話乙").checked).toBe(true);
    expect(backend.storedConversation(66).folder).not.toBe("proj");
    const failedPut = backend.requestsFor(/^\/api\/conversations\/66$/, "PUT").at(-1);
    expect(failedPut.body).toMatchObject({ folder: "proj" });
    expectBatchIdentity(failedPut);
  });

  it("畫面 user id 不是正整數時，批次刪除不開確認、不送請求，按鈕停用", async () => {
    const backend = createFakeBackend({
      user: { id: "nope", username: "tester", display_name: "測試使用者" },
      conversations: [convRow(55, "對話甲"), convRow(66, "對話乙")],
    });
    await mountOrchestrator({ backend });
    expect(screen.getByTestId("conv-bulk-relogin").textContent).toMatch(/重新登入/);
    expect(screen.getByTestId("conv-bulk-relogin").textContent).toMatch(/重新整理/);
    expect(screen.getByRole("button", { name: "多選" })).toBeDisabled();
    const bar = await openBulkFromDisabledEntry();
    fireEvent.click(convBox("對話甲"));
    fireEvent.click(convBox("對話乙"));
    const deleteBtn = within(bar).getByRole("button", { name: "刪除" });
    const moveBtn = within(bar).getByRole("button", { name: "移入" });
    expect(deleteBtn).toBeDisabled();
    expect(moveBtn).toBeDisabled();
    await invokeOnClick(deleteBtn);
    await invokeOnClick(moveBtn);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 30));
    });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByText("對話甲")).toBeInTheDocument();
    expect(screen.getByText("對話乙")).toBeInTheDocument();
    expect(convBox("對話甲").checked).toBe(true);
    expect(convBox("對話乙").checked).toBe(true);
    expect(backend.requests.filter(
      (r) => r.method === "DELETE" && /^\/api\/conversations\/\d+$/.test(r.path),
    )).toHaveLength(0);
    expect(backend.requests.filter(
      (r) => r.method === "PUT" && /^\/api\/conversations\/\d+$/.test(r.path),
    )).toHaveLength(0);
  });

  it("缺少 user id 時，本機與伺服器對話的批次刪除不確認、不改資料、也不送請求", async () => {
    const { backend, writesBefore, serverMessages } = await mountMissingUserMixed();
    const bar = await openBulkFromDisabledEntry();
    fireEvent.click(convBox("離線這則要留著"));
    fireEvent.click(convBox("對話甲"));
    const deleteBtn = within(bar).getByRole("button", { name: "刪除" });
    expect.soft(deleteBtn.disabled).toBe(true);
    expect.soft(screen.queryByTestId("conv-bulk-relogin")?.textContent ?? "").toMatch(/重新登入/);
    expect.soft(screen.queryByTestId("conv-bulk-relogin")?.textContent ?? "").toMatch(/重新整理/);
    await invokeOnClick(deleteBtn);
    const opened = await finishConfirmIfOpened("刪除對話", "刪除");
    expect.soft(opened).toBe(false);
    expect.soft(screen.queryByRole("dialog")).toBeNull();
    expect.soft(sidebarTitles("離線這則要留著")).toHaveLength(1);
    expect.soft(sidebarTitles("對話甲")).toHaveLength(1);
    expect.soft(screen.getByText("甲的回答")).toBeInTheDocument();
    expect.soft(backend.storedMessages(55).map((row) => row.content)).toEqual(serverMessages);
    expect.soft(backend.storedConversation(55).folder).toBe("all");
    expect.soft(conversationWrites(backend)).toHaveLength(writesBefore);
    if (sidebarTitles("離線這則要留著").length === 1) {
      await selectConversation("離線這則要留著");
      expect.soft(userMessageIncludes("離線這則要留著")).toBe(true);
    }
  });

  it("缺少 user id 時，本機與伺服器對話的批次移入不確認、不改資料、也不送請求", async () => {
    const { backend, writesBefore, serverMessages } = await mountMissingUserMixed();
    const bar = await openBulkFromDisabledEntry();
    fireEvent.click(convBox("離線這則要留著"));
    fireEvent.click(convBox("對話甲"));
    const moveBtn = within(bar).getByRole("button", { name: "移入" });
    const moveSelect = within(bar).getByLabelText("移到群組");
    expect.soft(moveBtn.disabled).toBe(true);
    expect.soft(moveSelect.disabled).toBe(true);
    expect.soft(screen.queryByTestId("conv-bulk-relogin")?.textContent ?? "").toMatch(/重新登入/);
    expect.soft(screen.queryByTestId("conv-bulk-relogin")?.textContent ?? "").toMatch(/重新整理/);
    const onChange = reactProps(moveSelect)?.onChange;
    if (typeof onChange === "function") {
      await act(async () => {
        onChange({ target: { value: "proj" }, preventDefault() {}, stopPropagation() {} });
      });
    }
    await invokeOnClick(moveBtn);
    const opened = await finishConfirmIfOpened("移入群組", "移入");
    expect.soft(opened).toBe(false);
    expect.soft(screen.queryByRole("dialog")).toBeNull();
    expect.soft(backend.storedConversation(55).folder).toBe("all");
    expect.soft(backend.storedMessages(55).map((row) => row.content)).toEqual(serverMessages);
    expect.soft(conversationWrites(backend)).toHaveLength(writesBefore);
    fireEvent.click(screen.getByRole("button", { name: "專案" }));
    expect.soft(sidebarTitles("離線這則要留著")).toHaveLength(0);
    expect.soft(sidebarTitles("對話甲")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "全部" }));
    expect.soft(sidebarTitles("離線這則要留著")).toHaveLength(1);
    expect.soft(sidebarTitles("對話甲")).toHaveLength(1);
    if (sidebarTitles("離線這則要留著").length === 1) {
      await selectConversation("離線這則要留著");
      expect.soft(userMessageIncludes("離線這則要留著")).toBe(true);
    }
  });

  it("確認期間開始串流的對話維持勾選且停用，不送刪除，結束後可直接再刪", async () => {
    const backend = createFakeBackend({
      conversations: [convRow(55, "對話甲"), convRow(66, "對話乙")],
    });
    backend.disableTitleGeneration();
    await mountOrchestrator({ backend });
    await selectConversation("對話甲");
    const bar = await enterBulk();
    fireEvent.click(convBox("對話甲"));
    fireEvent.click(convBox("對話乙"));
    fireEvent.click(within(bar).getByRole("button", { name: "刪除" }));
    const dialog = await screen.findByRole("dialog", { name: "刪除對話" });
    backend.enqueueManualStream();
    await sendText("慢慢回答");
    expect(await screen.findByLabelText("停止產生")).toBeInTheDocument();
    expect(convBox("對話甲").checked).toBe(true);
    expect(convBox("對話甲")).toBeDisabled();
    expect(screen.getByTestId("conv-bulk-count").textContent).toMatch(/已選 2/);
    expect(screen.getByTestId("conv-bulk-waiting").textContent).toMatch(/等待/);
    fireEvent.click(within(dialog).getByRole("button", { name: "刪除" }));
    await waitFor(() => {
      expect(sidebarTitles("對話乙")).toHaveLength(0);
    });
    expect(sidebarTitles("對話甲")).toHaveLength(1);
    expect(convBox("對話甲").checked).toBe(true);
    expect(convBox("對話甲")).toBeDisabled();
    expect(backend.requestsFor(/^\/api\/conversations\/55$/, "DELETE")).toHaveLength(0);
    expect(backend.requestsFor(/^\/api\/conversations\/66$/, "DELETE")).toHaveLength(1);
    expect(screen.getByTestId("conv-bulk-status").textContent).toMatch(/等待完成/);
    expect(screen.getByTestId("conv-bulk-count").textContent).toMatch(/已選 1/);
    backend.stream?.close();
    await waitForIdle();
    await waitFor(() => {
      expect(convBox("對話甲")).toBeEnabled();
    });
    expect(convBox("對話甲").checked).toBe(true);
    fireEvent.click(within(screen.getByTestId("conv-bulk-bar")).getByRole("button", { name: "刪除" }));
    const retry = await screen.findByRole("dialog", { name: "刪除對話" });
    expect(retry.textContent).toMatch(/1 則/);
    fireEvent.click(within(retry).getByRole("button", { name: "刪除" }));
    await waitFor(() => {
      expect(sidebarTitles("對話甲")).toHaveLength(0);
    });
    expect(backend.requestsFor(/^\/api\/conversations\/55$/, "DELETE")).toHaveLength(1);
  });

  it("排隊空位時才開始串流的對話維持勾選且停用，不送刪除，結束後可直接再刪", async () => {
    const backend = createFakeBackend({
      conversations: [1, 2, 3, 4, 5].map((id) => convRow(id, `對話${id}`, {
        updated_at: `2026-10-0${6 - id}T00:00:00Z`,
      })),
    });
    backend.disableTitleGeneration();
    const release = [];
    const seen = [];
    backend.route("DELETE", /^\/api\/conversations\/\d+$/, async (req) => {
      seen.push(Number(req.path.split("/").pop()));
      await new Promise((resolve) => release.push(resolve));
      return undefined;
    });
    await mountOrchestrator({ backend });
    await selectConversation("對話5");
    const bar = await enterBulk();
    fireEvent.click(within(bar).getByRole("button", { name: "全選" }));
    fireEvent.click(within(bar).getByRole("button", { name: "刪除" }));
    await confirmDialog("刪除對話", "刪除");
    await waitFor(() => {
      expect(seen).toHaveLength(4);
    });
    expect(seen).not.toContain(5);
    backend.enqueueManualStream();
    await sendText("慢慢回答");
    expect(await screen.findByLabelText("停止產生")).toBeInTheDocument();
    expect(convBox("對話5").checked).toBe(true);
    expect(convBox("對話5")).toBeDisabled();
    expect(screen.getByTestId("conv-bulk-waiting").textContent).toMatch(/等待/);
    expect(backend.requestsFor(/^\/api\/conversations\/5$/, "DELETE")).toHaveLength(0);
    const deadline = Date.now() + 5000;
    while (screen.queryByTestId("conv-bulk-status")?.textContent?.includes("等待完成") !== true) {
      if (Date.now() > deadline) {
        throw new Error(`排隊中的串流對話仍被刪 seen=${seen.join(",")}`);
      }
      const batch = release.splice(0);
      await act(async () => {
        batch.forEach((fn) => fn());
      });
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    expect(backend.requestsFor(/^\/api\/conversations\/5$/, "DELETE")).toHaveLength(0);
    expect(sidebarTitles("對話5")).toHaveLength(1);
    expect(convBox("對話5").checked).toBe(true);
    expect(convBox("對話5")).toBeDisabled();
    expect(screen.getByTestId("conv-bulk-count").textContent).toMatch(/已選 1/);
    for (const id of [1, 2, 3, 4]) {
      expect(sidebarTitles(`對話${id}`)).toHaveLength(0);
    }
    backend.stream?.close();
    await waitForIdle();
    await waitFor(() => {
      expect(convBox("對話5")).toBeEnabled();
    });
    expect(convBox("對話5").checked).toBe(true);
    fireEvent.click(within(screen.getByTestId("conv-bulk-bar")).getByRole("button", { name: "刪除" }));
    const retry = await screen.findByRole("dialog", { name: "刪除對話" });
    expect(retry.textContent).toMatch(/1 則/);
    fireEvent.click(within(retry).getByRole("button", { name: "刪除" }));
    await waitFor(() => {
      expect(seen).toContain(5);
    });
    expect(backend.requestsFor(/^\/api\/conversations\/5$/, "DELETE")).toHaveLength(1);
    const pending = release.splice(0);
    await act(async () => {
      pending.forEach((fn) => fn());
    });
    await waitFor(() => {
      expect(sidebarTitles("對話5")).toHaveLength(0);
    });
  });
});

describe("側欄多選的鍵盤、窄列與帳號切換", () => {
  const noop = () => {};

  function renderSidebar(props = {}) {
    const base = {
      conversations: [
        { id: 55, title: "對話甲", updatedAt: "2026-10-02T00:00:00Z", folder: "all", tags: [] },
        { id: 66, title: "對話乙", updatedAt: "2026-10-01T00:00:00Z", folder: "all", tags: [] },
      ],
      selectedConvId: null,
      onSelectConv: noop,
      onNewChat: noop,
      agents: [],
      onOpenServices: noop,
      onTaskCenter: noop,
      user: { id: 1, username: "tester", role: "user" },
      onLogout: noop,
      onOpenSettings: noop,
      collapsed: false,
      onToggleCollapsed: noop,
      folder: "all",
      setFolder: noop,
      folders: DEFAULT_FOLDERS,
      onBulkDelete: vi.fn(async () => ({ succeeded: [], failed: [] })),
      onBulkMove: vi.fn(async () => ({ succeeded: [], failed: [] })),
      busyConversationIds: [],
    };
    return render(
      <ConfirmProvider>
        <div style={{ width: 272 }}>
          <Sidebar {...base} {...props} />
        </div>
      </ConfirmProvider>,
    );
  }

  it("checkbox 是可聚焦的原生控制，換帳號或群組會清掉選取", () => {
    const view = renderSidebar();
    fireEvent.click(screen.getByRole("button", { name: "多選" }));
    const box = convBox("對話甲");
    expect(box.tagName).toBe("INPUT");
    expect(box.getAttribute("type")).toBe("checkbox");
    box.focus();
    expect(document.activeElement).toBe(box);
    fireEvent.click(box);
    expect(box.checked).toBe(true);
    expect(screen.getByTestId("conv-bulk-count").textContent).toMatch(/已選 1/);

    const bar = screen.getByTestId("conv-bulk-bar");
    expect(bar.style.flexWrap).toBe("wrap");
    expect(bar.style.overflow).not.toBe("hidden");
    for (const btn of within(bar).getAllByRole("button")) {
      expect(btn.style.position).not.toBe("absolute");
    }

    view.rerender(
      <ConfirmProvider>
        <div style={{ width: 272 }}>
          <Sidebar
            {...{
              conversations: [
                { id: 55, title: "對話甲", updatedAt: "2026-10-02T00:00:00Z", folder: "all", tags: [] },
                { id: 66, title: "對話乙", updatedAt: "2026-10-01T00:00:00Z", folder: "all", tags: [] },
              ],
              selectedConvId: null,
              onSelectConv: noop,
              onNewChat: noop,
              agents: [],
              onOpenServices: noop,
              onTaskCenter: noop,
              user: { id: 2, username: "other", role: "user" },
              onLogout: noop,
              onOpenSettings: noop,
              collapsed: false,
              onToggleCollapsed: noop,
              folder: "all",
              setFolder: noop,
              folders: DEFAULT_FOLDERS,
              onBulkDelete: vi.fn(async () => ({ succeeded: [], failed: [] })),
              onBulkMove: vi.fn(async () => ({ succeeded: [], failed: [] })),
              busyConversationIds: [],
            }}
          />
        </div>
      </ConfirmProvider>,
    );
    expect(screen.getByTestId("conv-bulk-count").textContent).toMatch(/已選 0/);
    expect(convBox("對話甲").checked).toBe(false);

    view.rerender(
      <ConfirmProvider>
        <div style={{ width: 272 }}>
          <Sidebar
            {...{
              conversations: [
                { id: 55, title: "對話甲", updatedAt: "2026-10-02T00:00:00Z", folder: "all", tags: [] },
                { id: 66, title: "對話乙", updatedAt: "2026-10-01T00:00:00Z", folder: "proj", tags: [] },
              ],
              selectedConvId: null,
              onSelectConv: noop,
              onNewChat: noop,
              agents: [],
              onOpenServices: noop,
              onTaskCenter: noop,
              user: { id: 2, username: "other", role: "user" },
              onLogout: noop,
              onOpenSettings: noop,
              collapsed: false,
              onToggleCollapsed: noop,
              folder: "proj",
              setFolder: noop,
              folders: FOLDERS,
              onBulkDelete: vi.fn(async () => ({ succeeded: [], failed: [] })),
              onBulkMove: vi.fn(async () => ({ succeeded: [], failed: [] })),
              busyConversationIds: [],
            }}
          />
        </div>
      </ConfirmProvider>,
    );
    expect(screen.queryByRole("checkbox", { name: "選取對話 對話甲" })).toBeNull();
    expect(screen.getByTestId("conv-bulk-count").textContent).toMatch(/已選 0/);
  });

  it("忙碌的對話禁選，全選只勾目前可見且可選的項目", () => {
    renderSidebar({ busyConversationIds: [55] });
    fireEvent.click(screen.getByRole("button", { name: "多選" }));
    const blocked = convBox("對話甲");
    expect(blocked).toBeDisabled();
    expect(screen.getByText("請等待完成")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "全選" }));
    expect(blocked.checked).toBe(false);
    expect(convBox("對話乙").checked).toBe(true);
    expect(screen.getByTestId("conv-bulk-count").textContent).toMatch(/已選 1/);
    fireEvent.click(screen.getByRole("button", { name: "取消全選" }));
    expect(convBox("對話乙").checked).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByTestId("conv-bulk-bar")).toBeNull();
    expect(screen.getByRole("button", { name: "多選" })).toBeInTheDocument();
  });

  it("已勾的對話開始串流後仍算已選且停用，全選不取消等待項，取消全選會清空", () => {
    const conversations = [
      { id: 55, title: "對話甲", updatedAt: "2026-10-02T00:00:00Z", folder: "all", tags: [] },
      { id: 66, title: "對話乙", updatedAt: "2026-10-01T00:00:00Z", folder: "all", tags: [] },
    ];
    const props = {
      conversations,
      selectedConvId: null,
      onSelectConv: noop,
      onNewChat: noop,
      agents: [],
      onOpenServices: noop,
      onTaskCenter: noop,
      user: { id: 1, username: "tester", role: "user" },
      onLogout: noop,
      onOpenSettings: noop,
      collapsed: false,
      onToggleCollapsed: noop,
      folder: "all",
      setFolder: noop,
      folders: DEFAULT_FOLDERS,
      onBulkDelete: vi.fn(async () => ({ succeeded: [], failed: [] })),
      onBulkMove: vi.fn(async () => ({ succeeded: [], failed: [] })),
      busyConversationIds: [],
    };
    const view = render(
      <ConfirmProvider>
        <div style={{ width: 272 }}>
          <Sidebar {...props} />
        </div>
      </ConfirmProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "多選" }));
    fireEvent.click(convBox("對話甲"));
    view.rerender(
      <ConfirmProvider>
        <div style={{ width: 272 }}>
          <Sidebar {...props} busyConversationIds={[55]} />
        </div>
      </ConfirmProvider>,
    );
    const waiting = convBox("對話甲");
    expect(waiting.checked).toBe(true);
    expect(waiting).toBeDisabled();
    expect(screen.getByTestId("conv-bulk-count").textContent).toMatch(/已選 1/);
    expect(screen.getByTestId("conv-bulk-waiting").textContent).toMatch(/1/);
    expect(screen.getByTestId("conv-bulk-waiting").textContent).toMatch(/等待/);
    expect(within(screen.getByTestId("conv-bulk-bar")).getByRole("button", { name: "刪除" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "全選" }));
    expect(convBox("對話甲").checked).toBe(true);
    expect(convBox("對話乙").checked).toBe(true);
    expect(screen.getByTestId("conv-bulk-count").textContent).toMatch(/已選 2/);
    fireEvent.click(screen.getByRole("button", { name: "取消全選" }));
    expect(convBox("對話甲").checked).toBe(false);
    expect(convBox("對話乙").checked).toBe(false);
    expect(screen.getByTestId("conv-bulk-count").textContent).toMatch(/已選 0/);
    expect(screen.queryByTestId("conv-bulk-waiting")).toBeNull();
  });

  it("多選方塊與移到群組接上平台樣式，原生名稱與選項不變", () => {
    renderSidebar({
      conversations: [
        { id: 55, title: "對話甲", updatedAt: "2026-10-02T00:00:00Z", folder: "all", tags: [] },
        { id: 66, title: "很長很長的群組名稱對話", updatedAt: "2026-10-01T00:00:00Z", folder: "all", tags: [] },
      ],
      busyConversationIds: [66],
      folders: FOLDERS,
    });
    fireEvent.click(screen.getByRole("button", { name: "多選" }));
    const box = convBox("對話甲");
    expect(box.tagName).toBe("INPUT");
    expect(box.getAttribute("type")).toBe("checkbox");
    expect(box.classList.contains("anila-bulk-check")).toBe(true);
    const label = box.closest("label");
    expect(label?.classList.contains("anila-bulk-check-hit")).toBe(true);
    box.focus();
    expect(document.activeElement).toBe(box);
    fireEvent.click(box);
    expect(box.checked).toBe(true);
    expect(screen.getByTestId("conv-bulk-count").textContent).toMatch(/已選 1/);

    const blocked = convBox("很長很長的群組名稱對話");
    expect(blocked).toBeDisabled();
    expect(blocked.classList.contains("anila-bulk-check")).toBe(true);

    const bar = screen.getByTestId("conv-bulk-bar");
    expect(bar.style.flexWrap).toBe("wrap");
    const select = within(bar).getByLabelText("移到群組");
    expect(select.tagName).toBe("SELECT");
    expect(select.getAttribute("aria-label")).toBe("移到群組");
    expect(select.classList.contains("anila-bulk-select")).toBe(true);
    expect(select.style.maxWidth).toBe("120px");
    expect(parseFloat(select.style.minWidth)).toBe(0);
    expect([...select.options].map((option) => option.tagName)).toEqual(["OPTION", "OPTION"]);
    fireEvent.change(select, { target: { value: "proj" } });
    expect(select.value).toBe("proj");
    const wrap = select.parentElement;
    expect(wrap?.classList.contains("anila-bulk-select-wrap")).toBe(true);
    expect(parseFloat(wrap?.style.minWidth)).toBe(0);
    expect(wrap?.style.maxWidth).toBe("120px");
    expect(wrap?.style.flex).toBe("1 1 72px");
    expect(select.nextElementSibling).toBe(wrap?.querySelector(".anila-bulk-select-chevron"));
    const chevron = select.nextElementSibling;
    expect(chevron?.getAttribute("aria-hidden")).toBe("true");
    expect(chevron?.style.pointerEvents).toBe("none");
    const moveLabel = select.closest("label");
    expect(moveLabel?.classList.contains("anila-bulk-select-label")).toBe(true);
    expect(moveLabel?.style.flexWrap).toBe("wrap");
    expect(moveLabel?.style.maxWidth).toBe("100%");
    expect(parseFloat(moveLabel?.style.minWidth)).toBe(0);
    expect(moveLabel?.style.flex).toBe("1 1 10rem");
  });

  it("搜尋與新資料夾只把文字框算進外框焦點，旁邊按鈕不算", () => {
    renderSidebar({ onCreateFolder: vi.fn() });
    const search = screen.getByRole("textbox", { name: "搜尋對話" });
    expect(search.classList.contains("anila-inset-field")).toBe(true);
    expect(parseFloat(search.style.minWidth)).toBe(0);
    const searchSurface = search.closest(".anila-inset-surface");
    expect(searchSurface).not.toBeNull();
    fireEvent.change(search, { target: { value: "甲" } });
    expect(search.value).toBe("甲");
    const clear = screen.getByRole("button", { name: "清除搜尋" });
    expect(searchSurface.contains(clear)).toBe(true);
    expect(clear.classList.contains("anila-inset-field")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "多選" }));
    expect(screen.getByRole("checkbox", { name: "選取對話 對話甲" }).classList.contains("anila-inset-field")).toBe(false);

    fireEvent.click(screen.getByTitle("新增資料夾"));
    const folder = screen.getByPlaceholderText("資料夾名稱");
    expect(folder.classList.contains("anila-inset-field")).toBe(true);
    expect(document.activeElement).toBe(folder);
    const pill = folder.closest(".anila-inset-surface");
    expect(pill).not.toBeNull();
    expect(pill).not.toBe(searchSurface);
    const cancel = screen.getByTitle("取消");
    expect(pill.contains(cancel)).toBe(true);
    expect(cancel.classList.contains("anila-inset-field")).toBe(false);
  });
});
