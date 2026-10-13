import { describe, expect, it } from "vitest";

import catalog from "../starterPrompts.data.json" with { type: "json" };
import {
  cjkSearchUnits,
  retrieveStarterDocument,
  starterReplyForQuestion,
  visibleStarterPrompts,
} from "../starterPrompts.js";

describe("starter prompt retrieval", () => {
  it("splits CJK the same way as the keyword fallback", () => {
    expect(cjkSearchUnits("會議室")).toEqual(["會議", "議室", "會議室"]);
    expect(cjkSearchUnits("會")).toEqual(["會"]);
    expect(cjkSearchUnits("示範文件裡，會議室每次最長可以借幾小時？")).toHaveLength(12);
  });

  it("shows only questions whose expected passage is the unique hit", () => {
    const visible = visibleStarterPrompts();
    expect(visible.map((item) => item.id)).toEqual([
      "meeting-hours",
      "retention-years",
      "visitor-photo",
    ]);
    expect(visible.map((item) => item.id)).not.toContain("parking-apply");
    for (const item of visible) {
      const hit = retrieveStarterDocument(item.q);
      expect(hit?.text).toContain(item.expect_span);
      expect(hit?.text).toContain(item.quote);
      expect(item.quote).toContain(item.expect_span);
    }
  });

  it("hides the parking question because every sample document ties", () => {
    const hidden = catalog.prompts.find((item) => item.id === "parking-apply");
    expect(retrieveStarterDocument(hidden.q)).toBeNull();
    expect(starterReplyForQuestion(hidden.q)).toBeNull();
  });

  it("quotes the retrieved sentence and does not invent a second source", () => {
    const meeting = visibleStarterPrompts().find((item) => item.id === "meeting-hours");
    const reply = starterReplyForQuestion(meeting.q);
    expect(reply).toContain("四小時");
    expect(reply).toContain("示範：會議室借用");
    expect(reply).toContain("合成範例");
    expect(reply).toContain("模型沒有參與");
    expect(starterReplyForQuestion(`  ${meeting.q}  `)).toBe(reply);
    expect(starterReplyForQuestion("會議室可以借多久")).toBeNull();
  });
});
