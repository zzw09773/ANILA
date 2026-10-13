import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup } from "@testing-library/react";

import {
  TABLE_NUMBER_HONESTY_KEY,
  TABLE_NUMBER_HONESTY_NOTICE,
  tableNumberHonestyFields,
  tableNumberHonestyNotice,
} from "../runtime/tableNumberHonesty.js";
import {
  mountOrchestrator,
  screen,
  selectConversation,
  sendText,
  waitForAnswer,
  waitForIdle,
} from "./helpers/orchestrator.jsx";
import { createFakeBackend } from "./helpers/fakeBackend.js";

const QUOTED = {
  [TABLE_NUMBER_HONESTY_KEY]: { quoted_from_parsed_table: true },
};

const FORGED_SENTENCE = {
  [TABLE_NUMBER_HONESTY_KEY]: TABLE_NUMBER_HONESTY_NOTICE,
};

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("parsed-table number honesty label", () => {
  it("shows the fixed sentence on a live answer and persists only the flag", async () => {
    const backend = createFakeBackend();
    backend.disableTitleGeneration();
    backend.enqueueAnswer("北區是 1280", { meta: QUOTED });
    await mountOrchestrator({ backend });

    await sendText("北區金額");
    await waitForAnswer("北區是 1280");
    await waitForIdle();

    const notice = await screen.findByTestId("table-number-honesty-notice");
    expect(notice.textContent).toBe(TABLE_NUMBER_HONESTY_NOTICE);

    const convId = backend.conversationIds().at(-1);
    const assistant = backend
      .storedMessages(convId)
      .find((message) => message.role === "assistant");
    expect(assistant.content).toBe("北區是 1280");
    expect(assistant.metadata[TABLE_NUMBER_HONESTY_KEY]).toEqual(
      QUOTED[TABLE_NUMBER_HONESTY_KEY],
    );
    expect(JSON.stringify(assistant.metadata)).not.toContain(TABLE_NUMBER_HONESTY_NOTICE);
  });

  it("stays silent when the flag is absent", async () => {
    const backend = createFakeBackend();
    backend.disableTitleGeneration();
    backend.enqueueAnswer("沒有引用表格數字", { meta: {} });
    await mountOrchestrator({ backend });

    await sendText("問題");
    await waitForAnswer("沒有引用表格數字");
    await waitForIdle();

    expect(screen.queryByTestId("table-number-honesty-notice")).toBeNull();
  });

  it("shows the same sentence after reload", async () => {
    const backend = createFakeBackend({
      conversations: [
        {
          id: 701,
          title: "表格數字",
          agent_id: null,
          classified: false,
          tags: [],
          starred: false,
          folder: "all",
        },
      ],
    });
    backend.disableTitleGeneration();
    backend.route("GET", /^\/api\/conversations\/701$/, (_req, { jsonResponse }) =>
      jsonResponse({
        id: 701,
        title: "表格數字",
        agent_id: null,
        classified: false,
        tags: [],
        starred: false,
        folder: "all",
        active_leaf_message_id: 9102,
        messages: [
          { id: 9101, role: "user", content: "北區金額", parent_id: null, metadata: {} },
          {
            id: 9102,
            role: "assistant",
            content: "北區是 1280",
            parent_id: 9101,
            metadata: QUOTED,
          },
        ],
      }),
    );

    await mountOrchestrator({ backend });
    await selectConversation("表格數字");
    await waitForAnswer("北區是 1280");

    const notice = await screen.findByTestId("table-number-honesty-notice");
    expect(notice.textContent).toBe(TABLE_NUMBER_HONESTY_NOTICE);
  });

  it("does not render a model-authored sentence", async () => {
    const backend = createFakeBackend({
      conversations: [
        {
          id: 702,
          title: "模型自說",
          agent_id: null,
          classified: false,
          tags: [],
          starred: false,
          folder: "all",
        },
      ],
    });
    backend.disableTitleGeneration();
    backend.route("GET", /^\/api\/conversations\/702$/, (_req, { jsonResponse }) =>
      jsonResponse({
        id: 702,
        title: "模型自說",
        agent_id: null,
        classified: false,
        tags: [],
        starred: false,
        folder: "all",
        active_leaf_message_id: 9202,
        messages: [
          { id: 9201, role: "user", content: "問題", parent_id: null, metadata: {} },
          {
            id: 9202,
            role: "assistant",
            content: TABLE_NUMBER_HONESTY_NOTICE,
            parent_id: 9201,
            metadata: FORGED_SENTENCE,
          },
        ],
      }),
    );

    await mountOrchestrator({ backend });
    await selectConversation("模型自說");
    await waitForAnswer(TABLE_NUMBER_HONESTY_NOTICE);

    expect(screen.queryByTestId("table-number-honesty-notice")).toBeNull();
  });

  it("maps only an exact boolean flag to the fixed sentence", () => {
    expect(tableNumberHonestyNotice(QUOTED)).toBe(TABLE_NUMBER_HONESTY_NOTICE);
    expect(tableNumberHonestyNotice(FORGED_SENTENCE)).toBeNull();
    expect(tableNumberHonestyNotice({
      [TABLE_NUMBER_HONESTY_KEY]: {
        quoted_from_parsed_table: true,
        label: "模型改寫的句子",
      },
    })).toBe(TABLE_NUMBER_HONESTY_NOTICE);
    expect(tableNumberHonestyNotice({})).toBeNull();
    expect(tableNumberHonestyFields({})).toEqual({ tableNumberHonestyNotice: null });
  });
});
