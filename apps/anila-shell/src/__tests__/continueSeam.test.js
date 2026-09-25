import { describe, expect, it } from "vitest";

import { joinContinuation } from "../runtime/continueSeam.js";

describe("joinContinuation", () => {
  it("drops a repeated tail so the join appears once", () => {
    const replay = "太陽系的形成過程".repeat(5);
    const prior = `這段答案在${replay}`;
    const extra = `${replay}，然後寫下水星。`;
    expect(joinContinuation(prior, extra)).toBe(
      `這段答案在${replay}，然後寫下水星。`,
    );
  });

  it("still trims the overlap when the continuation starts on a new line", () => {
    const replay = "太陽系的形成過程".repeat(5);
    const prior = `第一章講到${replay}`;
    const extra = `\n${replay}，接著第二章。`;
    expect(joinContinuation(prior, extra)).toBe(
      `第一章講到${replay}，接著第二章。`,
    );
  });

  it("does not glue unrelated sentences on a short shared ending", () => {
    expect(joinContinuation("答案結束。", "結束。下一題")).toBe(
      "答案結束。結束。下一題",
    );
  });

  it("continues a code fence cut mid-token without inserting a space", () => {
    expect(joinContinuation("```python\nprint(\"hel", "lo\")\n```")).toBe(
      "```python\nprint(\"hello\")\n```",
    );
  });

  it("keeps a shorter fence line inside a longer open fence", () => {
    const prior = "````\nconst marker = ";
    const extra = "```\nconst marker = \"tick\";\n````";
    expect(joinContinuation(prior, extra)).toBe(
      "````\nconst marker = ```\nconst marker = \"tick\";\n````",
    );
  });

  it("does not delete a code line both branches need", () => {
    const prior = "if (ready) {\n  return value;\n}\nreturn value;";
    const extra = "return value;\n";
    expect(joinContinuation(prior, extra)).toBe(`${prior}${extra}`);
  });

  it("inserts a space when an English word is cut before the next word", () => {
    expect(joinContinuation("The answer is", "42.")).toBe("The answer is 42.");
  });

  it("drops a repeated opening fence so a split block stays one block", () => {
    const prior = "程式如下：\n```js\nconst answer = \"";
    const extra = "```js\nconst answer = \"ok\";\n```\n完成。";
    const joined = joinContinuation(prior, extra);
    expect(joined).toBe(
      "程式如下：\n```js\nconst answer = \"const answer = \"ok\";\n```\n完成。",
    );
    expect(joined.match(/```/g)).toEqual(["```", "```"]);
  });
});
