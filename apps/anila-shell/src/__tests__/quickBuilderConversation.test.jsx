import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup } from "@testing-library/react";
import {
  act,
  clickNewChat,
  createFakeBackend,
  fireEvent,
  historyOf,
  mountOrchestrator,
  screen,
  selectConversation,
  sendText,
  waitForAnswer,
  waitForIdle,
} from "./helpers/orchestrator.jsx";
import { headerValue } from "./helpers/fakeBackend.js";

const AGENT_ID = "quick-33-v1";
const AGENTS = [{
  id: AGENT_ID,
  db_id: 733,
  name: "快製查詢助手",
  short: "快製",
  description: "已通過平台核准的合成快製助手",
  capabilities: {},
  requires_encryption: false,
}];
const FIRST_QUESTION = "這次會議名稱是秋季測試，請記住。";
const FIRST_ANSWER = "已記住這次會議名稱是秋季測試。";
const SECOND_QUESTION = "剛才的會議名稱是什麼？";

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function chooseQuickAssistant() {
  const auto = screen.queryByRole("button", { name: /ANILA auto/ });
  if (auto) {
    await act(async () => { fireEvent.click(auto); });
    await act(async () => { fireEvent.click(screen.getByText("快製查詢助手")); });
  }
}
function turns(backend) {
  return backend.requestsFor("/v1/chat/completions", "POST").filter(request => request.body.stream === true);
}
async function firstTurn() {
  const backend = createFakeBackend({ agents: AGENTS });
  backend.disableTitleGeneration().enqueueAnswer(FIRST_ANSWER);
  const app = await mountOrchestrator({ backend });
  await chooseQuickAssistant();
  await sendText(FIRST_QUESTION);
  await waitForAnswer(FIRST_ANSWER);
  await waitForIdle();
  return { backend, app };
}

describe("快製助手沿用 Shell 多輪歷史", () => {
  it("第二輪送出原問題、第一輪回答與追問，仍指定同一個快製助手", async () => {
    const { backend } = await firstTurn();
    backend.enqueueAnswer("剛才的會議名稱是秋季測試。");
    await sendText(SECOND_QUESTION);
    await waitForAnswer("剛才的會議名稱是秋季測試。");
    await waitForIdle();
    expect(historyOf(backend, 1)).toEqual([
      `user:${FIRST_QUESTION}`,
      `assistant:${FIRST_ANSWER}`,
      `user:${SECOND_QUESTION}`,
    ]);
    const requests = turns(backend);
    expect(backend.requestsFor("/api/conversations", "POST")[0].body.agent_id).toBe(733);
    expect(requests.map(request => request.body.model)).toEqual([AGENT_ID, AGENT_ID]);
    expect(headerValue(requests[1], "X-ANILA-Conversation-Id")).toBe(headerValue(requests[0], "X-ANILA-Conversation-Id"));
    expect(headerValue(requests[1], "X-ANILA-Task-Id")).toBeTruthy();
  });

  it("讀回已儲存對話後，追問仍包含原問題與回答", async () => {
    const { backend, app } = await firstTurn();
    app.unmount();
    await mountOrchestrator({ backend });
    await selectConversation(FIRST_QUESTION);
    backend.enqueueAnswer("重新開啟後仍知道會議名稱。");
    await sendText(SECOND_QUESTION);
    await waitForAnswer("重新開啟後仍知道會議名稱。");
    await waitForIdle();
    expect(historyOf(backend, 1)).toEqual([
      `user:${FIRST_QUESTION}`,
      `assistant:${FIRST_ANSWER}`,
      `user:${SECOND_QUESTION}`,
    ]);
    expect(turns(backend)[1].body.model).toBe(AGENT_ID);
  });

  it("另開新對話時，不帶入前一份快製助手對話內容", async () => {
    const { backend } = await firstTurn();
    await clickNewChat();
    await chooseQuickAssistant();
    backend.enqueueAnswer("這是獨立的新對話。");
    await sendText("這是另一場會議，請重新開始。");
    await waitForAnswer("這是獨立的新對話。");
    await waitForIdle();
    expect(historyOf(backend, 1)).toEqual(["user:這是另一場會議，請重新開始。"]);
    const requests = turns(backend);
    expect(requests[1].body.model).toBe(AGENT_ID);
    expect(headerValue(requests[1], "X-ANILA-Conversation-Id")).not.toBe(headerValue(requests[0], "X-ANILA-Conversation-Id"));
  });

  it("對話先於助手目錄載入時，阻擋送出並於目錄到達後回復原助手", async () => {
    const { backend, app } = await firstTurn();
    app.unmount();
    let releaseAgents;
    backend.route("GET", "/v1/agents", async (_, { jsonResponse }) => {
      await new Promise(resolve => { releaseAgents = resolve; });
      return jsonResponse({ data: AGENTS });
    }, { once: true });
    await mountOrchestrator({ backend });
    await selectConversation(FIRST_QUESTION);
    await sendText(SECOND_QUESTION);
    expect(turns(backend)).toHaveLength(1);
    expect(screen.getByText("正在讀取這份對話使用的助手，請稍後再送出。")).toBeTruthy();
    await act(async () => { releaseAgents(); });
    backend.enqueueAnswer("目錄載入後使用原助手回答。");
    await sendText(SECOND_QUESTION);
    await waitForAnswer("目錄載入後使用原助手回答。");
    expect(turns(backend)[1].body.model).toBe(AGENT_ID);
  });

  it("既有對話切換為自動路由會先保存null，切回快製也保存數字id並於重載後恢復", async () => {
    const { backend, app } = await firstTurn();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: /快製查詢助手 快製/ })); });
    await act(async () => { fireEvent.click(screen.getByText("ANILA", { selector: "div" })); });
    backend.enqueueAnswer("改用自動路由回答。");
    await sendText("改用自動路由。");
    await waitForAnswer("改用自動路由回答。");
    await waitForIdle();
    const first = turns(backend)[0];
    const convId = Number(headerValue(first, "X-ANILA-Conversation-Id"));
    let mutations = backend.requestsFor(`/api/conversations/${convId}`, "PUT").filter(r => Object.hasOwn(r.body, "agent_id"));
    expect(mutations.at(-1).body.agent_id).toBeNull();
    expect(headerValue(mutations.at(-1), "X-ANILA-Expected-User-ID")).toBeTruthy();
    await chooseQuickAssistant();
    backend.enqueueAnswer("改回快製助手回答。");
    await sendText("改回快製助手。");
    await waitForAnswer("改回快製助手回答。");
    await waitForIdle();
    mutations = backend.requestsFor(`/api/conversations/${convId}`, "PUT").filter(r => Object.hasOwn(r.body, "agent_id"));
    expect(mutations.at(-1).body.agent_id).toBe(733);
    app.unmount();
    await mountOrchestrator({ backend });
    await selectConversation(FIRST_QUESTION);
    backend.enqueueAnswer("重載後仍由快製助手回答。");
    await sendText("現在使用哪個助手？");
    await waitForAnswer("重載後仍由快製助手回答。");
    expect(turns(backend).at(-1).body.model).toBe(AGENT_ID);
  });

  it("助手切換保存被拒絕時保留草稿與原target，不派發聊天", async () => {
    const { backend } = await firstTurn();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: /快製查詢助手 快製/ })); });
    await act(async () => { fireEvent.click(screen.getByText("ANILA", { selector: "div" })); });
    backend.route("PUT", /^\/api\/conversations\/\d+$/, (request, { errorResponse }) => {
      if (Object.hasOwn(request.body, "agent_id")) return errorResponse(409, "登入身分已變更，請重新登入");
    });
    await sendText("這份草稿應該保留。");
    expect(turns(backend)).toHaveLength(1);
    expect(screen.getByRole("textbox", { name: "傳訊息給 ANILA" }).value).toBe("這份草稿應該保留。");
    expect(screen.getByText("登入身分已變更，請重新登入")).toBeTruthy();
    const convId = Number(headerValue(turns(backend)[0], "X-ANILA-Conversation-Id"));
    expect(backend.storedConversation(convId).agent_id).toBe(733);
  });

  it("建立對話遇身分衝突時保留草稿，不能降級成離線派發", async () => {
    const backend = createFakeBackend({ agents: AGENTS });
    backend.route("POST", /^\/api\/conversations$/, (_, { errorResponse }) => errorResponse(409, "登入帳號已變更，請重新整理後再操作"));
    await mountOrchestrator({ backend });
    await chooseQuickAssistant();
    await sendText("不能送給另一個登入帳號的草稿。");
    expect(turns(backend)).toHaveLength(0);
    expect(screen.getByRole("textbox", { name: "傳訊息給 ANILA" }).value).toBe("不能送給另一個登入帳號的草稿。");
    expect(screen.getByText("登入帳號已變更，請重新整理後再操作")).toBeTruthy();
  });
});
