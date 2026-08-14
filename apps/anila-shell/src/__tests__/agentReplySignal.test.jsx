import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup } from "@testing-library/react";

import { agentReplyMetaFields } from "../app.jsx";
import {
  AGENT_REPLY_OBSERVATION_KEY,
  AGENT_REPLY_SHORT_NOTICE,
  agentReplyNotice,
} from "../runtime/agentReplySignal.js";
import {
  mountOrchestrator,
  screen,
  selectConversation,
  sendText,
  waitForAnswer,
  waitForIdle,
} from "./helpers/orchestrator.jsx";
import { createFakeBackend } from "./helpers/fakeBackend.js";

const SHORT_META = {
  [AGENT_REPLY_OBSERVATION_KEY]: {
    completion_tokens: 121,
    usage_source: "reported",
    short_reply: true,
  },
};

const LONG_META = {
  [AGENT_REPLY_OBSERVATION_KEY]: {
    completion_tokens: 219,
    usage_source: "reported",
  },
};

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("registered-agent short-reply observation", () => {
  it("renders one observational sentence and persists the complete observation on live SSE", async () => {
    const backend = createFakeBackend();
    backend.disableTitleGeneration();
    backend.enqueueAnswer("這是原樣回答", { meta: SHORT_META });
    await mountOrchestrator({ backend });

    await sendText("一個長問題");
    await waitForAnswer("這是原樣回答");
    await waitForIdle();

    const notice = await screen.findByTestId("message-agent-reply-notice");
    expect(notice.textContent).toBe(AGENT_REPLY_SHORT_NOTICE);
    expect(notice.textContent).not.toMatch(/攔截|拒絕|封鎖/);

    const convId = backend.conversationIds().at(-1);
    const assistant = backend
      .storedMessages(convId)
      .find((message) => message.role === "assistant");
    expect(assistant.content).toBe("這是原樣回答");
    expect(assistant.metadata[AGENT_REPLY_OBSERVATION_KEY]).toEqual(
      SHORT_META[AGENT_REPLY_OBSERVATION_KEY],
    );
  });

  it("live SSE with absent or unknown observation stays silent", async () => {
    const backend = createFakeBackend();
    backend.disableTitleGeneration();
    backend.enqueueAnswer("一般回答", { meta: { unrelated: true } });
    await mountOrchestrator({ backend });

    await sendText("問題");
    await waitForAnswer("一般回答");
    await waitForIdle();

    expect(screen.queryByTestId("message-agent-reply-notice")).toBeNull();
  });

  it("reload maps an absent observation to no rendered notice", async () => {
    const backend = createFakeBackend({
      conversations: [
        {
          id: 501,
          title: "沒有觀測的對話",
          agent_id: null,
          classified: false,
          tags: [],
          starred: false,
          folder: "all",
        },
      ],
    });
    backend.disableTitleGeneration();
    backend.route("GET", /^\/api\/conversations\/501$/, (_req, { jsonResponse }) =>
      jsonResponse({
        id: 501,
        title: "沒有觀測的對話",
        agent_id: null,
        classified: false,
        tags: [],
        starred: false,
        folder: "all",
        active_leaf_message_id: 9002,
        messages: [
          { id: 9001, role: "user", content: "問題", parent_id: null, metadata: {} },
          {
            id: 9002,
            role: "assistant",
            content: "重新載入的回答",
            parent_id: 9001,
            metadata: { unrelated: true },
          },
        ],
      }),
    );

    await mountOrchestrator({ backend });
    await selectConversation("沒有觀測的對話");
    await waitForAnswer("重新載入的回答");

    expect(screen.queryByTestId("message-agent-reply-notice")).toBeNull();
  });

  it("does not render a long-reply observation on reload", async () => {
    const backend = createFakeBackend({
      conversations: [
        {
          id: 502,
          title: "正常長回答",
          agent_id: null,
          classified: false,
          tags: [],
          starred: false,
          folder: "all",
        },
      ],
    });
    backend.disableTitleGeneration();
    backend.route("GET", /^\/api\/conversations\/502$/, (_req, { jsonResponse }) =>
      jsonResponse({
        id: 502,
        title: "正常長回答",
        agent_id: null,
        classified: false,
        tags: [],
        starred: false,
        folder: "all",
        active_leaf_message_id: 9012,
        messages: [
          { id: 9011, role: "user", content: "問題", parent_id: null, metadata: {} },
          {
            id: 9012,
            role: "assistant",
            content: "正常回答",
            parent_id: 9011,
            metadata: LONG_META,
          },
        ],
      }),
    );

    await mountOrchestrator({ backend });
    await selectConversation("正常長回答");
    await waitForAnswer("正常回答");

    expect(screen.queryByTestId("message-agent-reply-notice")).toBeNull();
  });

  it("keeps the notice helper observational and the two-seam mapping silent for unknown meta", () => {
    expect(agentReplyNotice(SHORT_META)).toBe(AGENT_REPLY_SHORT_NOTICE);
    expect(agentReplyNotice({})).toBeNull();
    expect(agentReplyNotice({ [AGENT_REPLY_OBSERVATION_KEY]: { short_reply: true } })).toBeNull();
    expect(agentReplyMetaFields({})).toEqual({
      agentReplyObservation: undefined,
      agentReplyNotice: null,
    });
  });
});
