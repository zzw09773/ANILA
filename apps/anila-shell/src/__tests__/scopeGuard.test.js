import { describe, expect, it } from "vitest";

import {
  localChatReply,
  matchOutOfScopeAction,
  outOfScopeReply,
} from "../runtime/scopeGuard.js";

describe("out-of-scope action word list", () => {
  it("catches send-email and approve requests", () => {
    expect(matchOutOfScopeAction("幫我寄信給窗口")?.kinds).toEqual(["email"]);
    expect(matchOutOfScopeAction("請幫我發一封信")?.kinds).toEqual(["email"]);
    expect(matchOutOfScopeAction("Please send an email to the desk")?.kinds).toEqual(["email"]);
    expect(matchOutOfScopeAction("幫我核准這張申請")?.kinds).toEqual(["approve"]);
    expect(matchOutOfScopeAction("麻煩你立刻批准")?.kinds).toContain("approve");
    expect(matchOutOfScopeAction("Can you approve this request?")?.kinds).toEqual(["approve"]);
  });

  it("does not catch questions about the rules", () => {
    for (const text of [
      "核准流程是什麼？",
      "寄信要附回條嗎？",
      "申請核准要幾天？",
      "誰可以核准這類申請？",
      "規定說不可以發送郵件嗎？",
      "what is the approval process?",
    ]) {
      expect(matchOutOfScopeAction(text), text).toBeNull();
    }
  });

  it("gives a next step the product can actually do", () => {
    const email = outOfScopeReply(matchOutOfScopeAction("幫我寄信"));
    expect(email).toContain("我不能代你寄出信件");
    expect(email).toContain("附加檔案");
    expect(email).toContain("分享");
    expect(email).toContain("交給同事接手");
    expect(email).not.toContain("請稍後再試");

    const approve = outOfScopeReply(matchOutOfScopeAction("幫我核准"));
    expect(approve).toContain("我不能代你核准");
    expect(approve).toContain("附加檔案");
    expect(approve).toContain("交給同事接手");
    expect(approve).not.toContain("請稍後再試");

    const both = localChatReply("幫我寄信，順便幫我核准");
    expect(both).toContain("寄出");
    expect(both).toContain("核准");
    expect(both.match(/交給同事接手/g)).toHaveLength(1);
  });

  it("leaves ordinary questions for the model", () => {
    expect(localChatReply("今天會議室怎麼登記？")).toBeNull();
  });
});
