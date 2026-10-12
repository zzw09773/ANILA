import { describe, expect, it } from "vitest";

import {
  SYNTHETIC_EXAMPLE,
  annotateTranslation,
  buildTranslationPrompt,
  checkTranslation,
} from "../runtime/translationFidelity.js";

const GOOD = "感測器校正必須在 12 小時內完成。不要略過感測器校正。備品重量為 5 公斤。";

describe("翻譯忠實度：原文對譯文", () => {
  it("合成範例同時標出數字、單位、否定，以及未定譯", () => {
    const report = checkTranslation(SYNTHETIC_EXAMPLE.source, SYNTHETIC_EXAMPLE.translation);
    const number = report.mismatches.find((item) => item.kind === "number");
    const unit = report.mismatches.find((item) => item.kind === "unit");
    const negation = report.mismatches.find((item) => item.kind === "negation");
    expect(number.message).toContain("13");
    expect(number.message).toContain("12");
    expect(number.translationStart).toBeGreaterThanOrEqual(0);
    expect(unit.message).toContain("5 g");
    expect(unit.message).toContain("5 kg");
    expect(negation.message).toContain("Do not skip");
    const calibration = report.unsettled.find((item) => item.term === "calibration");
    expect(calibration.variants).toEqual(expect.arrayContaining(["校正", "校準"]));
    expect(calibration.message).toContain("未定譯");
    expect(report.unsettled.map((item) => item.term)).not.toContain("sensor");
  });

  it("同一篇譯文數字、單位、否定和譯名都一致時不標", () => {
    const report = checkTranslation(SYNTHETIC_EXAMPLE.source, GOOD);
    expect(report.mismatches).toEqual([]);
    expect(report.unsettled).toEqual([]);
  });

  it("中文數字、百分比、攝氏和千分位與原文是同一個值", () => {
    expect(checkTranslation("Finish within 12 hours.", "請在十二小時內完成。").mismatches).toEqual([]);
    expect(checkTranslation("The rate is 12%.", "比率是百分之十二。").mismatches).toEqual([]);
    expect(checkTranslation("Keep it at 37°C.", "維持攝氏 37 度。").mismatches).toEqual([]);
    expect(checkTranslation("Ship 1,000 units.", "出貨 1000 件。").mismatches).toEqual([]);
  });

  it("譯文多出來的數字要標出來", () => {
    const report = checkTranslation("Wait 5 days.", "等待 5 天，另外 2 天。");
    expect(report.mismatches.map((item) => item.message).join(" ")).toContain("2 天");
  });

  it("非常、未來、不得不、No. 不當成否定對不上", () => {
    expect(checkTranslation("This is very important.", "這非常重要。").mismatches).toEqual([]);
    expect(checkTranslation("The next 3 days.", "未來 3 天。").mismatches).toEqual([]);
    expect(checkTranslation("You must finish.", "你不得不完成。").mismatches).toEqual([]);
    expect(checkTranslation("No. 12 is ready.", "第12項已備妥。").mismatches).toEqual([]);
  });

  it("不得對上沒有否定的英文句子", () => {
    const report = checkTranslation("You must finish.", "你不得完成。");
    expect(report.mismatches.map((item) => item.kind)).toContain("negation");
  });

  it("同一個詞兩種譯法標未定譯，前後一致則不標", () => {
    const bad = checkTranslation(
      "The report is ready. The report is late.",
      "報告已經好了。報表晚了。",
    );
    expect(bad.unsettled.map((item) => item.term)).toEqual(["report"]);
    const good = checkTranslation(
      "The report is ready. The report is late.",
      "報告已經好了。報告晚了。",
    );
    expect(good.unsettled).toEqual([]);
  });

  it("中文同一個詞翻成英文時，兩句都用同一個詞就不標未定譯", () => {
    const report = checkTranslation(
      "感測器校正必須重做。完成感測器校正後記錄結果。",
      "Sensor calibration must be repeated. After sensor calibration, record the result.",
    );
    expect(report.unsettled).toEqual([]);
    expect(report.mismatches).toEqual([]);
  });

  it("紅框落在譯文裡對不上的那一段", () => {
    const report = checkTranslation(SYNTHETIC_EXAMPLE.source, SYNTHETIC_EXAMPLE.translation);
    const segments = annotateTranslation(SYNTHETIC_EXAMPLE.translation, report.mismatches);
    const boxed = segments.filter((segment) => segment.hits.length).map((segment) => segment.text).join("");
    expect(boxed).toContain("13");
    expect(boxed).toContain("5 g");
    expect(boxed).toContain("可以略過");
  });

  it("提示詞只要求完整翻譯，不要求模型自己檢查", () => {
    const prompt = buildTranslationPrompt("Hello sensor.", "繁體中文（臺灣）");
    expect(prompt).toContain("完整翻譯");
    expect(prompt).toContain("Hello sensor.");
    expect(prompt).not.toContain("未定譯");
    expect(prompt).not.toContain("紅框");
    expect(prompt).not.toContain("檢查");
    expect(prompt).not.toMatch(/qwen|deepseek|openai|anthropic/i);
  });
});
