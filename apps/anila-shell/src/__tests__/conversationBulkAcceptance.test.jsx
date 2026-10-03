import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, fireEvent, waitFor, within, cleanup } from "@testing-library/react";
import {
  createFakeBackend,
  mountOrchestrator,
  selectConversation,
} from "./helpers/orchestrator.jsx";

const A_ID = 55;
const B_ID = 66;
const A_TITLE = '驗收甲';
const B_TITLE = '驗收乙';
const A_ANSWER = '甲回答';
const B_ANSWER = '乙回答';

function makeBackend() {
  const backend = createFakeBackend({
    conversations: [
      { id: A_ID, title: A_TITLE, folder: "all", tags: [], starred: false, classified: false, updated_at: "2026-10-02T00:00:00Z", created_at: "2026-10-01T00:00:00Z" },
      { id: B_ID, title: B_TITLE, folder: "all", tags: [], starred: false, classified: false, updated_at: "2026-10-01T00:00:00Z", created_at: "2026-09-30T00:00:00Z" },
    ],
    uiSettings: {
      folders: [
        { id: "all", name: "全部" },
        { id: "starred", name: "已加星" },
        { id: "proj", name: "專案" },
      ],
    },
  });
  backend.seedMessages(A_ID, [
    { id: 550, role: "user", content: "甲問題" },
    { id: 551, role: "assistant", content: A_ANSWER },
  ]);
  backend.seedMessages(B_ID, [
    { id: 660, role: "user", content: "乙問題" },
    { id: 661, role: "assistant", content: B_ANSWER },
  ]);
  return backend;
}

/** 記錄 fetch 的 method+url，DELETE/PUT 斷言全部走這一份，與 fake 內部請求log 無關。 */
function trackFetch(backend) {
  const tracked = [];
  const original = backend.fetch.bind(backend);
  backend.fetch = (...args) => {
    const url = typeof args[0] === "string" ? args[0] : String(args[0]?.url ?? "");
    const method = String(args[1]?.method || "GET").toUpperCase();
    tracked.push({ method, url });
    return original(...args);
  };
  return tracked;
}

function countCalls(tracked, method, id) {
  return tracked.filter(
    (c) => c.method === method && new RegExp(`/api/conversations/${id}(?:$|[/?])`).test(c.url),
  ).length;
}

function enterBulk() {
  fireEvent.click(screen.getByRole("button", { name: "多選" }));
  return screen.findByTestId("conv-bulk-bar");
}

function convBox(title) {
  return screen.getByRole("checkbox", { name: `選取對話 ${title}` });
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('conversation bulk acceptance (real App)', () => {
  it("刪除「作用中」的對話：對 active id 送出恰好一次 DELETE，未選取的不受波及", async () => {
    const backend = makeBackend();
    const tracked = trackFetch(backend);
    await mountOrchestrator({ backend });

    await selectConversation(B_TITLE);
    await waitFor(() => expect(screen.getByText(B_ANSWER)).toBeInTheDocument());

    await selectConversation(A_TITLE);
    await waitFor(() => expect(screen.getByText(A_ANSWER)).toBeInTheDocument());

    const bar = await enterBulk();
    fireEvent.click(convBox(A_TITLE));
    fireEvent.click(within(bar).getByRole("button", { name: "刪除" }));

    const dialog = await screen.findByRole("dialog", { name: "刪除對話" });
    fireEvent.click(within(dialog).getByRole("button", { name: "刪除" }));

    await waitFor(() => expect(screen.queryByText(A_TITLE)).not.toBeInTheDocument());
    await waitFor(() => expect(screen.queryByText(A_ANSWER)).not.toBeInTheDocument());

    expect(countCalls(tracked, "DELETE", A_ID)).toBe(1);
    expect(countCalls(tracked, "DELETE", B_ID)).toBe(0);
    expect(backend.conversationIds()).not.toContain(A_ID);
    expect(backend.conversationIds()).toContain(B_ID);
    // 畫面上回到新對話狀態（頂部標題退回「新對話」）。
    await waitFor(() => expect(screen.getAllByText("新對話").length).toBeGreaterThan(0));

    await selectConversation(B_TITLE);
    await waitFor(() => expect(screen.getByText(B_ANSWER)).toBeInTheDocument());
    expect(screen.queryByText(A_ANSWER)).not.toBeInTheDocument();
  });

  it("刪除/移入按取消：零 DELETE、零 PUT，對話與選取都還在", async () => {
    const backend = makeBackend();
    const tracked = trackFetch(backend);
    await mountOrchestrator({ backend });

    const bar = await enterBulk();
    fireEvent.click(convBox(A_TITLE));
    fireEvent.click(convBox(B_TITLE));
    fireEvent.click(within(bar).getByRole("button", { name: "刪除" }));

    const dialog = await screen.findByRole("dialog", { name: "刪除對話" });
    expect(dialog.textContent).toMatch(/2 則對話/);
    expect(dialog.textContent).toMatch(/無法復原/);
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));

    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "刪除對話" })).not.toBeInTheDocument(),
    );
    const barAfter = await screen.findByTestId("conv-bulk-bar");
    await waitFor(() =>
      expect(within(barAfter).getByRole("button", { name: "刪除" })).toBeEnabled(),
    );

    expect(countCalls(tracked, "DELETE", A_ID)).toBe(0);
    expect(countCalls(tracked, "DELETE", B_ID)).toBe(0);

    fireEvent.change(within(barAfter).getByLabelText("移到群組"), { target: { value: "proj" } });
    fireEvent.click(within(barAfter).getByRole("button", { name: "移入" }));

    const moveDialog = await screen.findByRole("dialog", { name: "移入群組" });
    fireEvent.click(within(moveDialog).getByRole("button", { name: "取消" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "移入群組" })).not.toBeInTheDocument(),
    );

    expect(countCalls(tracked, "PUT", A_ID)).toBe(0);
    expect(countCalls(tracked, "PUT", B_ID)).toBe(0);
    expect(backend.conversationIds()).toContain(A_ID);
    expect(backend.conversationIds()).toContain(B_ID);
    expect(screen.getByText(A_TITLE)).toBeInTheDocument();
    expect(screen.getByText(B_TITLE)).toBeInTheDocument();
  });
});
