import { describe, it, expect, vi, afterEach } from "vitest";
import {
  selectAdoptMessages,
  promoteAdoptedAnswer,
} from "../runtime/adoptCompare.js";
import { adoptConversation, ANILA_UI_ORIGIN } from "../runtime/conversations.js";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("selectAdoptMessages", () => {
  it("rejects empty / incomplete columns", () => {
    expect(selectAdoptMessages([]).ok).toBe(false);
    expect(selectAdoptMessages([{ role: "user", text: "hi" }]).ok).toBe(false);
    expect(
      selectAdoptMessages([
        { role: "user", text: "hi" },
        { role: "assistant", text: "...", streaming: true },
      ]).ok,
    ).toBe(false);
    expect(
      selectAdoptMessages([
        { role: "user", text: "hi" },
        { role: "assistant", text: "", error: "boom" },
      ]).ok,
    ).toBe(false);
  });

  it("picks the paired user for the latest completed assistant (multi-round)", () => {
    const picked = selectAdoptMessages([
      { role: "user", text: "Q1" },
      { role: "assistant", text: "A1", streaming: false },
      { role: "user", text: "Q2" },
      { role: "assistant", text: "A2", streaming: false, classified: true },
    ]);
    expect(picked.ok).toBe(true);
    expect(picked.user.text).toBe("Q2");
    expect(picked.assistant.text).toBe("A2");
    expect(picked.assistant.classified).toBe(true);
  });
});

describe("promoteAdoptedAnswer", () => {
  it("POSTs /api/conversations/adopt with promoted on-screen text", async () => {
    const authRequest = vi.fn(async () => ({
      id: 42,
      classified: true,
      classification_level: "密",
      messages: [
        { id: 1, role: "user", content: "比較問題", parent_id: null },
        { id: 2, role: "assistant", content: "採用答案", parent_id: 1 },
      ],
    }));

    const detail = await promoteAdoptedAnswer({
      authRequest,
      adoptConversation,
      agentId: "secret-agent",
      agentDisplayName: "密等 Agent",
      msgs: [
        { role: "user", text: "比較問題" },
        {
          role: "assistant",
          text: "採用答案",
          streaming: false,
          classified: true,
          trace: [{ label: "done" }],
          latencyMs: 12,
        },
      ],
      makeTitle: (t) => `標題:${t}`,
    });

    expect(detail.id).toBe(42);
    expect(detail.classified).toBe(true);
    expect(authRequest).toHaveBeenCalledTimes(1);
    const [path, init] = authRequest.mock.calls[0];
    expect(path).toBe("/api/conversations/adopt");
    expect(init.method).toBe("POST");
    const body = JSON.parse(init.body);
    expect(body).toMatchObject({
      title: "標題:比較問題",
      origin: ANILA_UI_ORIGIN,
      agent_name: "secret-agent",
      user_content: "比較問題",
      assistant_content: "採用答案",
      assistant_agent_name: "密等 Agent",
      assistant_latency_ms: 12,
    });
    expect(body.classified).toBeUndefined();
    expect(body.assistant_metadata?.classified).toBe(true);
    expect(body.assistant_metadata?.trace).toEqual([{ label: "done" }]);
  });

  it("does not invent a local conversation when the API fails", async () => {
    const authRequest = vi.fn(async () => {
      throw new Error("伺服器拒絕");
    });
    const onSuccess = vi.fn();
    await expect(
      promoteAdoptedAnswer({
        authRequest,
        adoptConversation,
        agentId: "a",
        msgs: [
          { role: "user", text: "Q" },
          { role: "assistant", text: "A", streaming: false },
        ],
      }).then((d) => {
        onSuccess(d);
        return d;
      }),
    ).rejects.toThrow("伺服器拒絕");
    expect(onSuccess).not.toHaveBeenCalled();
  });

  it("fails visibly on precondition errors without calling the API", async () => {
    const authRequest = vi.fn();
    await expect(
      promoteAdoptedAnswer({
        authRequest,
        adoptConversation,
        agentId: "a",
        msgs: [],
      }),
    ).rejects.toThrow(/尚無內容/);
    expect(authRequest).not.toHaveBeenCalled();
  });
});

/**
 * Mutant notes (actually reverted during verification):
 * - Backend latch: comment out `_latch_agent_policy_on_conversation` →
 *   test_adopt_latches_from_agent_policy RED (classified stays false).
 * - Backend tree: skip assistant append_message →
 *   test_adopt_persists_tree_and_survives_reload RED (len != 2).
 * - Frontend: make promoteAdoptedAnswer return `{id:"cv-…"}` without calling
 *   authRequest → POSTs /adopt + failure tests RED.
 */
describe("adopt server-truth contract", () => {
  it("promoteAdoptedAnswer yields a numeric conversation id from the API", async () => {
    const authRequest = vi.fn(async () => ({
      id: 7,
      classified: true,
      classification_level: "密",
      messages: [
        { id: 1, role: "user", content: "q", parent_id: null },
        { id: 2, role: "assistant", content: "a", parent_id: 1 },
      ],
    }));
    const detail = await promoteAdoptedAnswer({
      authRequest,
      adoptConversation,
      agentId: "x",
      msgs: [
        { role: "user", text: "q" },
        { role: "assistant", text: "a", streaming: false },
      ],
    });
    expect(typeof detail.id).toBe("number");
    expect(Number.isInteger(detail.id)).toBe(true);
    expect(authRequest.mock.calls[0][0]).toBe("/api/conversations/adopt");
    // Must not invent classified on the request body — server decides.
    const body = JSON.parse(authRequest.mock.calls[0][1].body);
    expect(body.classified).toBeUndefined();
  });
});
