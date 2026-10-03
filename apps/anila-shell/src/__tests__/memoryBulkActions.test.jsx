// 記憶頁對話摘要：多選刪除與一次全刪。
// 斷言打到的是確認文案、請求與畫面上留下的列，不是函式回傳自比。
import { describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";

import { ConfirmProvider } from "../confirm.jsx";
import { MemoryTab } from "../memory.jsx";

function summary(id) {
  return {
    id,
    conversation_id: 3,
    summary: `摘要${id}`,
    updated_at: "2026-09-25T00:00:00Z",
  };
}

function renderTab(authRequest, extra = {}) {
  return render(
    <ConfirmProvider>
      <MemoryTab authRequest={authRequest} userId={1} {...extra} />
    </ConfirmProvider>,
  );
}

function createAuth(initial) {
  const state = {
    summaries: initial.map((row) => ({ ...row })),
    facts: [],
    pref: "",
    calls: [],
  };
  const authRequest = vi.fn(async (path, options = {}) => {
    const method = options.method || "GET";
    state.calls.push({ path, method, body: options.body, headers: options.headers || {} });
    if (path === "/api/memory/preference" && method === "GET") return { text: state.pref };
    if (path === "/api/memory/facts" && method === "GET") {
      return { total: state.facts.length, facts: state.facts };
    }
    if (path === "/api/memory/summaries" && method === "GET") {
      if (state.onListSummaries) return state.onListSummaries();
      return { total: state.summaries.length, items: state.summaries.map((row) => ({ ...row })) };
    }
    if (path === "/api/memory/summaries/bulk-delete" && method === "POST") {
      const ids = JSON.parse(options.body).ids;
      if (state.onBulk) return state.onBulk(ids);
      state.summaries = state.summaries.filter((row) => !ids.includes(row.id));
      return { deleted: ids.length };
    }
    if (path === "/api/memory/summaries" && method === "DELETE") {
      if (state.onClear) return state.onClear();
      const deleted = state.summaries.length;
      state.summaries = [];
      return { deleted };
    }
    if (path.startsWith("/api/memory/summaries/") && method === "DELETE") {
      const id = Number(path.split("/").pop());
      state.summaries = state.summaries.filter((row) => row.id !== id);
      return { deleted: 1 };
    }
    throw new Error(`unexpected ${method} ${path}`);
  });
  return { state, authRequest };
}

async function ready() {
  expect(await screen.findByRole("button", { name: "多選" })).toBeInTheDocument();
}

function box(id) {
  return screen.getByRole("checkbox", { name: `選取摘要 摘要${id}` });
}

function expectScreenUser(call, id = "1") {
  expect(call.headers["X-ANILA-Expected-User-ID"]).toBe(String(id));
}

describe("對話摘要多選與全部刪除", () => {
  it("多選刪除只打 bulk-delete，確認文案限於對話摘要，失敗項留在畫面上", async () => {
    const { state, authRequest } = createAuth([summary(1), summary(2), summary(3)]);
    state.onBulk = (ids) => {
      expect(ids).toEqual([1, 2]);
      expect(ids.every((id) => typeof id === "number" && Number.isInteger(id) && id > 0)).toBe(true);
      throw new Error("這批刪不掉");
    };
    renderTab(authRequest);
    await ready();
    expect(screen.getByText("摘要1")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "多選" }));
    const bar = screen.getByTestId("memory-summary-bulk-bar");
    expect(bar.style.flexWrap).toBe("wrap");
    const first = box(1);
    expect(first.tagName).toBe("INPUT");
    expect(first.getAttribute("type")).toBe("checkbox");
    first.focus();
    expect(document.activeElement).toBe(first);
    fireEvent.click(first);
    fireEvent.click(box(2));
    expect(screen.getByTestId("memory-summary-count").textContent).toMatch(/已選 2/);
    fireEvent.click(within(bar).getByRole("button", { name: "全選" }));
    expect(screen.getByTestId("memory-summary-count").textContent).toMatch(/已選 3/);
    fireEvent.click(within(bar).getByRole("button", { name: "取消全選" }));
    fireEvent.click(box(1));
    fireEvent.click(box(2));

    fireEvent.click(within(bar).getByRole("button", { name: "刪除所選" }));
    fireEvent.click(within(bar).getByRole("button", { name: "刪除所選" }));
    const dialogs = screen.getAllByRole("dialog");
    expect(dialogs).toHaveLength(1);
    expect(dialogs[0].textContent).toMatch(/2 則對話摘要/);
    expect(dialogs[0].textContent).toMatch(/不會刪除原始對話/);
    expect(dialogs[0].textContent).toMatch(/事實/);
    expect(dialogs[0].textContent).toMatch(/對話片段/);
    expect(dialogs[0].textContent).toMatch(/回覆偏好/);
    expect(dialogs[0].textContent).toMatch(/無法復原/);
    fireEvent.click(within(dialogs[0]).getByRole("button", { name: "刪除" }));

    expect(await screen.findByTestId("memory-summary-bulk-status")).toHaveTextContent(/仍保留/);
    expect(screen.getByText("摘要1")).toBeInTheDocument();
    expect(screen.getByText("摘要2")).toBeInTheDocument();
    expect(screen.getByText("摘要3")).toBeInTheDocument();
    expect(screen.getByTestId("memory-summary-bulk-status").textContent).not.toMatch(/已刪除 [123] 則/);
    const bulkCalls = state.calls.filter((c) => c.path === "/api/memory/summaries/bulk-delete");
    expect(bulkCalls).toHaveLength(1);
    expect(JSON.parse(bulkCalls[0].body).ids).toEqual([1, 2]);
    expectScreenUser(bulkCalls[0]);
    expect(state.calls.some((c) => c.path === "/api/memory/facts" && c.method === "DELETE")).toBe(false);
    expect(state.calls.some((c) => c.path === "/api/memory/chunks" && c.method === "DELETE")).toBe(false);
    expect(state.calls.some((c) => c.path === "/api/memory/preference" && c.method === "DELETE")).toBe(false);
    expect(state.calls.some((c) => c.path === "/api/memory/summaries" && c.method === "DELETE")).toBe(false);
  });

  it("多選成功只移除成功的摘要，單筆刪除按鈕仍在", async () => {
    const { state, authRequest } = createAuth([summary(9), summary(10), summary(11)]);
    renderTab(authRequest);
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "多選" }));
    fireEvent.click(box(9));
    fireEvent.click(box(10));
    fireEvent.click(screen.getByRole("button", { name: "刪除所選" }));
    const dialog = await screen.findByRole("dialog", { name: "刪除對話摘要" });
    fireEvent.click(within(dialog).getByRole("button", { name: "刪除" }));
    await waitFor(() => {
      expect(screen.queryByText("摘要9")).toBeNull();
    });
    expect(screen.queryByText("摘要10")).toBeNull();
    expect(screen.getByText("摘要11")).toBeInTheDocument();
    expect(screen.getByTestId("memory-summary-delete-11")).toBeInTheDocument();
    const bulk = state.calls.filter((c) => c.path === "/api/memory/summaries/bulk-delete");
    expect(bulk).toHaveLength(1);
    expect(JSON.parse(bulk[0].body).ids).toEqual([9, 10]);
    expectScreenUser(bulk[0]);
    expect(state.calls.some((c) => c.path === "/api/memory/summaries/9")).toBe(false);
  });

  it("全部刪除走單一 clear，失敗時摘要都留著", async () => {
    const { state, authRequest } = createAuth([summary(1), summary(2)]);
    state.onClear = () => {
      throw new Error("清空失敗");
    };
    renderTab(authRequest);
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "全部刪除" }));
    const dialog = await screen.findByRole("dialog", { name: "刪除全部對話摘要" });
    expect(dialog.textContent).toMatch(/全部 2 則對話摘要/);
    expect(dialog.textContent).toMatch(/不會刪除原始對話/);
    expect(dialog.textContent).toMatch(/事實/);
    expect(dialog.textContent).toMatch(/對話片段/);
    expect(dialog.textContent).toMatch(/回覆偏好/);
    expect(dialog.textContent).toMatch(/無法復原/);
    fireEvent.click(within(dialog).getByRole("button", { name: "全部刪除" }));
    expect(await screen.findByTestId("memory-summary-bulk-status")).toHaveTextContent(/仍保留/);
    expect(screen.getByText("摘要1")).toBeInTheDocument();
    expect(screen.getByText("摘要2")).toBeInTheDocument();
    const clearCalls = state.calls.filter((c) => c.path === "/api/memory/summaries" && c.method === "DELETE");
    expect(clearCalls).toHaveLength(1);
    expectScreenUser(clearCalls[0]);
    expect(clearCalls[0].body).toBeUndefined();
    expect(state.calls.some((c) => c.path === "/api/memory/summaries/bulk-delete")).toBe(false);

    state.onClear = () => {
      const deleted = state.summaries.length;
      state.summaries = [];
      return { deleted };
    };
    fireEvent.click(screen.getByRole("button", { name: "全部刪除" }));
    const again = await screen.findByRole("dialog", { name: "刪除全部對話摘要" });
    fireEvent.click(within(again).getByRole("button", { name: "全部刪除" }));
    await waitFor(() => {
      expect(screen.queryByText("摘要1")).toBeNull();
    });
    expect(screen.queryByText("摘要2")).toBeNull();
    expect(screen.getByText(/目前還沒有對話摘要/)).toBeInTheDocument();
  });

  it("超過 200 則分批序列刪除，失敗的那一批留著且不報全成功", async () => {
    const rows = Array.from({ length: 201 }, (_v, i) => summary(i + 1));
    const { state, authRequest } = createAuth(rows);
    let releaseFirst;
    const firstGate = new Promise((resolve) => {
      releaseFirst = resolve;
    });
    const seen = [];
    state.onBulk = async (ids) => {
      seen.push(ids);
      expect(ids.length).toBeLessThanOrEqual(200);
      expect(new Set(ids).size).toBe(ids.length);
      if (seen.length === 1) {
        await firstGate;
        state.summaries = state.summaries.filter((row) => !ids.includes(row.id));
        return { deleted: ids.length };
      }
      throw new Error("第二批失敗");
    };
    renderTab(authRequest);
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "多選" }));
    fireEvent.click(screen.getByRole("button", { name: "全選" }));
    expect(screen.getByTestId("memory-summary-count").textContent).toMatch(/已選 201/);
    fireEvent.click(screen.getByRole("button", { name: "刪除所選" }));
    const dialog = await screen.findByRole("dialog", { name: "刪除對話摘要" });
    expect(dialog.textContent).toMatch(/201 則對話摘要/);
    fireEvent.click(within(dialog).getByRole("button", { name: "刪除" }));

    await waitFor(() => {
      expect(seen.length).toBe(1);
    });
    await new Promise((r) => setTimeout(r, 30));
    expect(seen.length).toBe(1);
    expect(seen[0]).toHaveLength(200);
    releaseFirst();

    expect(await screen.findByTestId("memory-summary-bulk-status")).toHaveTextContent(/已刪除 200 則對話摘要/);
    expect(screen.getByTestId("memory-summary-bulk-status").textContent).toMatch(/1 則失敗/);
    expect(screen.getByTestId("memory-summary-bulk-status").textContent).toMatch(/仍保留/);
    expect(screen.getByTestId("memory-summary-bulk-status").textContent).not.toMatch(/已刪除 201/);
    expect(screen.queryByText("摘要1")).toBeNull();
    expect(screen.getByText("摘要201")).toBeInTheDocument();
    expect(seen).toHaveLength(2);
    expect(seen[1]).toEqual([201]);
    const bulkCalls = state.calls.filter((c) => c.path === "/api/memory/summaries/bulk-delete");
    expect(bulkCalls).toHaveLength(2);
    expectScreenUser(bulkCalls[0]);
    expectScreenUser(bulkCalls[1]);
    expect(JSON.parse(bulkCalls[0].body).ids).toHaveLength(200);
  });

  it("回傳刪除數與送出不一致時，整批都留著", async () => {
    const { state, authRequest } = createAuth([summary(1), summary(2)]);
    state.onBulk = (ids) => ({ deleted: ids.length - 1 });
    renderTab(authRequest);
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "多選" }));
    fireEvent.click(screen.getByRole("button", { name: "全選" }));
    fireEvent.click(screen.getByRole("button", { name: "刪除所選" }));
    const dialog = await screen.findByRole("dialog", { name: "刪除對話摘要" });
    fireEvent.click(within(dialog).getByRole("button", { name: "刪除" }));
    expect(await screen.findByTestId("memory-summary-bulk-status")).toHaveTextContent(/仍保留/);
    expect(screen.getByText("摘要1")).toBeInTheDocument();
    expect(screen.getByText("摘要2")).toBeInTheDocument();
    expect(screen.getByTestId("memory-summary-bulk-status").textContent).not.toMatch(/已刪除 2/);
  });

  it("批次確認或刪除進行中，單筆刪除不會送出；取消後可以再刪", async () => {
    const { state, authRequest } = createAuth([summary(1), summary(2)]);
    let release;
    const gate = new Promise((resolve) => {
      release = resolve;
    });
    state.onBulk = async (ids) => {
      await gate;
      state.summaries = state.summaries.filter((row) => !ids.includes(row.id));
      return { deleted: ids.length };
    };
    renderTab(authRequest);
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "多選" }));
    fireEvent.click(box(1));
    fireEvent.click(screen.getByRole("button", { name: "刪除所選" }));
    const dialog = await screen.findByRole("dialog", { name: "刪除對話摘要" });
    expect(screen.getByTestId("memory-summary-delete-2")).toBeDisabled();
    fireEvent.click(screen.getByTestId("memory-summary-delete-2"));
    expect(state.calls.filter((c) => /^\/api\/memory\/summaries\/\d+$/.test(c.path) && c.method === "DELETE")).toHaveLength(0);

    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    await waitFor(() => {
      expect(screen.getByTestId("memory-summary-delete-2")).toBeEnabled();
    });
    expect(state.calls.filter((c) => c.path === "/api/memory/summaries/bulk-delete")).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "刪除所選" }));
    const again = await screen.findByRole("dialog", { name: "刪除對話摘要" });
    fireEvent.click(within(again).getByRole("button", { name: "刪除" }));
    await waitFor(() => {
      expect(state.calls.filter((c) => c.path === "/api/memory/summaries/bulk-delete")).toHaveLength(1);
    });
    expect(screen.getByTestId("memory-summary-delete-2")).toBeDisabled();
    fireEvent.click(screen.getByTestId("memory-summary-delete-2"));
    expect(state.calls.filter((c) => c.path === "/api/memory/summaries/2" && c.method === "DELETE")).toHaveLength(0);
    release();
    await waitFor(() => {
      expect(screen.queryByText("摘要1")).toBeNull();
    });
    expect(screen.getByText("摘要2")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId("memory-summary-delete-2")).toBeEnabled();
    });
    fireEvent.click(screen.getByTestId("memory-summary-delete-2"));
    const single = await screen.findByRole("dialog", { name: "刪除摘要" });
    fireEvent.click(within(single).getByRole("button", { name: "刪除" }));
    await waitFor(() => {
      expect(state.calls.filter((c) => c.path === "/api/memory/summaries/2" && c.method === "DELETE")).toHaveLength(1);
    });
  });

  it("全部刪除回傳不是非負整數時不報成功，勾選仍在", async () => {
    const { state, authRequest } = createAuth([summary(1), summary(2)]);
    state.onClear = () => ({ deleted: 1.5 });
    renderTab(authRequest);
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "多選" }));
    fireEvent.click(box(1));
    fireEvent.click(screen.getByRole("button", { name: "全部刪除" }));
    const dialog = await screen.findByRole("dialog", { name: "刪除全部對話摘要" });
    fireEvent.click(within(dialog).getByRole("button", { name: "全部刪除" }));
    expect(await screen.findByTestId("memory-summary-bulk-status")).toHaveTextContent(/仍保留/);
    expect(screen.getByTestId("memory-summary-bulk-status").textContent).not.toMatch(/已刪除全部/);
    expect(screen.getByText("摘要1")).toBeInTheDocument();
    expect(screen.getByText("摘要2")).toBeInTheDocument();
    expect(box(1).checked).toBe(true);
    const clearCalls = state.calls.filter((c) => c.path === "/api/memory/summaries" && c.method === "DELETE");
    expect(clearCalls).toHaveLength(1);
    expectScreenUser(clearCalls[0]);
  });

  it("換了 authRequest 之後，舊的摘要清單不會蓋上新的", async () => {
    let release;
    let markStarted;
    const started = new Promise((resolve) => {
      markStarted = resolve;
    });
    const gate = new Promise((resolve) => {
      release = resolve;
    });
    const first = createAuth([summary(1)]);
    let listed = 0;
    first.state.onListSummaries = async () => {
      listed += 1;
      if (listed === 1) {
        markStarted();
        await gate;
      }
      return { total: 1, items: [summary(1)] };
    };
    const second = createAuth([summary(9)]);
    const view = render(
      <ConfirmProvider>
        <MemoryTab authRequest={first.authRequest} userId={1} />
      </ConfirmProvider>,
    );
    await started;
    view.rerender(
      <ConfirmProvider>
        <MemoryTab authRequest={second.authRequest} userId={1} />
      </ConfirmProvider>,
    );
    expect(await screen.findByText("摘要9")).toBeInTheDocument();
    release();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 40));
    });
    expect(screen.getByText("摘要9")).toBeInTheDocument();
    expect(screen.queryByText("摘要1")).toBeNull();
  });

  it("沒有 userId 時，新的批次控制停用並提示重新登入，單筆刪除仍不帶 expected header", async () => {
    const { state, authRequest } = createAuth([summary(1), summary(2)]);
    render(
      <ConfirmProvider>
        <MemoryTab authRequest={authRequest} />
      </ConfirmProvider>,
    );
    await ready();
    expect(screen.getByRole("button", { name: "多選" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "全部刪除" })).toBeDisabled();
    expect(screen.getByTestId("memory-summary-relogin").textContent).toMatch(/重新登入/);
    expect(screen.getByTestId("memory-summary-relogin").textContent).toMatch(/重新整理/);
    fireEvent.click(screen.getByRole("button", { name: "全部刪除" }));
    fireEvent.click(screen.getByRole("button", { name: "多選" }));
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(state.calls.filter((c) => c.path === "/api/memory/summaries" && c.method === "DELETE")).toHaveLength(0);
    expect(state.calls.filter((c) => c.path === "/api/memory/summaries/bulk-delete")).toHaveLength(0);
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.click(screen.getByTestId("memory-summary-delete-1"));
    const dialog = await screen.findByRole("dialog", { name: "刪除摘要" });
    fireEvent.click(within(dialog).getByRole("button", { name: "刪除" }));
    await waitFor(() => {
      expect(state.calls.filter((c) => c.path === "/api/memory/summaries/1" && c.method === "DELETE")).toHaveLength(1);
    });
    const single = state.calls.find((c) => c.path === "/api/memory/summaries/1" && c.method === "DELETE");
    expect(single.headers["X-ANILA-Expected-User-ID"]).toBeUndefined();
  });

  it("後端 409 時摘要與勾選都留著，狀態顯示登入帳號已變更，且不報全成功", async () => {
    const { state, authRequest } = createAuth([summary(1), summary(2)]);
    state.onBulk = () => {
      const err = new Error("登入帳號已變更，請重新整理後再操作");
      err.status = 409;
      throw err;
    };
    state.onClear = () => {
      const err = new Error("登入帳號已變更，請重新整理後再操作");
      err.status = 409;
      throw err;
    };
    renderTab(authRequest);
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "多選" }));
    fireEvent.click(box(1));
    fireEvent.click(box(2));
    fireEvent.click(screen.getByRole("button", { name: "刪除所選" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "刪除對話摘要" })).getByRole("button", { name: "刪除" }));
    expect(await screen.findByTestId("memory-summary-bulk-status")).toHaveTextContent(/登入帳號已變更/);
    expect(screen.getByTestId("memory-summary-bulk-status").textContent).toMatch(/仍保留/);
    expect(screen.getByTestId("memory-summary-bulk-status").textContent).not.toMatch(/已刪除 [12] 則/);
    expect(screen.getByText("摘要1")).toBeInTheDocument();
    expect(screen.getByText("摘要2")).toBeInTheDocument();
    expect(box(1).checked).toBe(true);
    expect(box(2).checked).toBe(true);
    const bulk = state.calls.filter((c) => c.path === "/api/memory/summaries/bulk-delete");
    expect(bulk).toHaveLength(1);
    expect(JSON.parse(bulk[0].body).ids).toEqual([1, 2]);
    expectScreenUser(bulk[0]);

    fireEvent.click(screen.getByRole("button", { name: "全部刪除" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "刪除全部對話摘要" })).getByRole("button", { name: "全部刪除" }));
    expect(await screen.findByTestId("memory-summary-bulk-status")).toHaveTextContent(/登入帳號已變更/);
    expect(screen.getByTestId("memory-summary-bulk-status").textContent).toMatch(/仍保留/);
    expect(screen.getByTestId("memory-summary-bulk-status").textContent).not.toMatch(/已刪除全部/);
    expect(screen.getByText("摘要1")).toBeInTheDocument();
    expect(screen.getByText("摘要2")).toBeInTheDocument();
    expect(box(1).checked).toBe(true);
    const clear = state.calls.filter((c) => c.path === "/api/memory/summaries" && c.method === "DELETE");
    expect(clear).toHaveLength(1);
    expectScreenUser(clear[0]);
  });

  it("第一批成功、下一批 409 後不再送舊身份，成功數保留且失敗仍勾選", async () => {
    const rows = Array.from({ length: 401 }, (_v, i) => summary(i + 1));
    const { state, authRequest } = createAuth(rows);
    const seen = [];
    state.onBulk = (ids) => {
      seen.push(ids.slice());
      if (seen.length === 1) {
        state.summaries = state.summaries.filter((row) => !ids.includes(row.id));
        return { deleted: ids.length };
      }
      const err = new Error("登入帳號已變更，請重新整理後再操作");
      err.status = 409;
      throw err;
    };
    renderTab(authRequest);
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "多選" }));
    fireEvent.click(screen.getByRole("button", { name: "全選" }));
    fireEvent.click(screen.getByRole("button", { name: "刪除所選" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "刪除對話摘要" })).getByRole("button", { name: "刪除" }));
    expect(await screen.findByTestId("memory-summary-bulk-status")).toHaveTextContent(/已刪除 200 則對話摘要/);
    expect(screen.getByTestId("memory-summary-bulk-status").textContent).toMatch(/201 則失敗/);
    expect(screen.getByTestId("memory-summary-bulk-status").textContent).toMatch(/登入帳號已變更/);
    expect(screen.getByTestId("memory-summary-bulk-status").textContent).toMatch(/仍保留/);
    expect(screen.getByTestId("memory-summary-bulk-status").textContent).not.toMatch(/已刪除 401/);
    expect(screen.queryByText("摘要1")).toBeNull();
    expect(screen.getByText("摘要201")).toBeInTheDocument();
    expect(screen.getByText("摘要401")).toBeInTheDocument();
    expect(box(201).checked).toBe(true);
    expect(box(401).checked).toBe(true);
    expect(seen).toHaveLength(2);
    expect(seen[0]).toHaveLength(200);
    expect(seen[1]).toHaveLength(200);
    const bulk = state.calls.filter((c) => c.path === "/api/memory/summaries/bulk-delete");
    expect(bulk).toHaveLength(2);
    expectScreenUser(bulk[0]);
    expectScreenUser(bulk[1]);
  });

  it("userId 改變後，晚到的 clear 與後續 bulk 不能清掉新清單，也不再送舊身份", async () => {
    let releaseClear;
    const clearGate = new Promise((resolve) => {
      releaseClear = resolve;
    });
    const firstClear = createAuth([summary(1), summary(2)]);
    firstClear.state.onClear = async () => {
      await clearGate;
      firstClear.state.summaries = [];
      return { deleted: 2 };
    };
    const secondClear = createAuth([summary(1), summary(9)]);
    const clearView = render(
      <ConfirmProvider>
        <MemoryTab authRequest={firstClear.authRequest} userId={1} />
      </ConfirmProvider>,
    );
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "全部刪除" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "刪除全部對話摘要" })).getByRole("button", { name: "全部刪除" }));
    await waitFor(() => {
      expect(firstClear.state.calls.filter((c) => c.path === "/api/memory/summaries" && c.method === "DELETE")).toHaveLength(1);
    });
    expectScreenUser(firstClear.state.calls.find((c) => c.path === "/api/memory/summaries" && c.method === "DELETE"));
    clearView.rerender(
      <ConfirmProvider>
        <MemoryTab authRequest={secondClear.authRequest} userId={2} />
      </ConfirmProvider>,
    );
    expect(await screen.findByText("摘要9")).toBeInTheDocument();
    releaseClear();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 40));
    });
    expect(screen.getByText("摘要1")).toBeInTheDocument();
    expect(screen.getByText("摘要9")).toBeInTheDocument();
    expect(screen.queryByText(/已刪除全部/)).toBeNull();
    expect(secondClear.state.calls.filter((c) => c.method === "DELETE")).toHaveLength(0);
    cleanup();

    const rows = Array.from({ length: 201 }, (_v, i) => summary(i + 1));
    const firstBulk = createAuth(rows);
    let releaseBulk;
    const bulkGate = new Promise((resolve) => {
      releaseBulk = resolve;
    });
    const seen = [];
    firstBulk.state.onBulk = async (ids) => {
      seen.push(ids.slice());
      if (seen.length === 1) await bulkGate;
      firstBulk.state.summaries = firstBulk.state.summaries.filter((row) => !ids.includes(row.id));
      return { deleted: ids.length };
    };
    const secondBulk = createAuth([summary(1), summary(9000)]);
    const bulkView = render(
      <ConfirmProvider>
        <MemoryTab authRequest={firstBulk.authRequest} userId={1} />
      </ConfirmProvider>,
    );
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "多選" }));
    fireEvent.click(screen.getByRole("button", { name: "全選" }));
    fireEvent.click(screen.getByRole("button", { name: "刪除所選" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "刪除對話摘要" })).getByRole("button", { name: "刪除" }));
    await waitFor(() => {
      expect(seen).toHaveLength(1);
    });
    expectScreenUser(firstBulk.state.calls.find((c) => c.path === "/api/memory/summaries/bulk-delete"));
    bulkView.rerender(
      <ConfirmProvider>
        <MemoryTab authRequest={secondBulk.authRequest} userId={2} />
      </ConfirmProvider>,
    );
    expect(await screen.findByText("摘要9000")).toBeInTheDocument();
    expect(screen.getByText("摘要1")).toBeInTheDocument();
    releaseBulk();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 40));
    });
    expect(seen).toHaveLength(1);
    expect(firstBulk.state.calls.filter((c) => c.path === "/api/memory/summaries/bulk-delete")).toHaveLength(1);
    expect(secondBulk.state.calls.filter((c) => c.path === "/api/memory/summaries/bulk-delete")).toHaveLength(0);
    expect(screen.getByText("摘要1")).toBeInTheDocument();
    expect(screen.getByText("摘要9000")).toBeInTheDocument();
  });
});
