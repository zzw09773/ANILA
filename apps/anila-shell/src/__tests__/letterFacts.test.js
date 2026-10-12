import { describe, expect, it } from "vitest";

import { compareLetterRewrite, extractLetterFacts } from "../runtime/letterFacts.js";

function flags(original, rewrite) {
  const compared = compareLetterRewrite(original, rewrite);
  return compared.changed.map((fact) => ({ kind: fact.kind, raw: fact.raw }));
}

function kinds(text) {
  return extractLetterFacts(text).map((fact) => fact.kind);
}

describe("信件事實比對", () => {
  it("同一天的不同寫法不標", () => {
    expect(flags("會議訂在2026年3月2日。", "會議訂在2026/03/02。")).toEqual([]);
    expect(flags("會議訂在民國115年3月2日。", "會議訂在2026-03-02。")).toEqual([]);
    expect(flags("會議訂在2026年3月2日。", "會議訂在3月2日。")).toEqual([]);
  });

  it("改寫多寫出年份要標成日期", () => {
    const changed = flags("請於3月2日前回覆。", "請於2026年3月2日前回覆。");
    expect(changed).toEqual([{ kind: "date", raw: "2026年3月2日" }]);
  });

  it("改了日期就標改寫裡的新日期", () => {
    expect(flags("3月2日借用場地。", "3月5日借用場地。")).toEqual([
      { kind: "date", raw: "3月5日" },
    ]);
  });

  it("金額寫法不同但數值相同不標", () => {
    expect(flags("費用新臺幣1,200元。", "費用1200元。")).toEqual([]);
    expect(flags("費用壹仟貳佰元。", "費用 NT$1,200。")).toEqual([]);
    expect(flags("費用新臺幣1,200元整。", "費用1,200元。")).toEqual([]);
  });

  it("新增或改動的金額要標", () => {
    expect(flags("費用1,200元。", "費用1,200元，保證金500元。")).toEqual([
      { kind: "amount", raw: "500元" },
    ]);
    expect(flags("費用1,200元。", "費用1,500元。")).toEqual([
      { kind: "amount", raw: "1,500元" },
    ]);
  });

  it("相同金額多寫一次算新增", () => {
    expect(flags("費用1,200元。", "費用1,200元，另收1,200元。")).toEqual([
      { kind: "amount", raw: "1,200元" },
    ]);
  });

  it("工作天的寫法不同不標，天數改了要標", () => {
    expect(flags("請於5個工作天內回覆。", "請於五個工作日內回覆。")).toEqual([]);
    expect(flags("請於5個工作天內回覆。", "請於3個工作天內回覆。")).toEqual([
      { kind: "deadline", raw: "3個工作天內" },
    ]);
  });

  it("日期沒變、期限前後對調時只標那個字", () => {
    expect(flags("最晚3月5日前確認。", "最晚3月5日後確認。")).toEqual([
      { kind: "deadline", raw: "後" },
    ]);
  });

  it("條號和文號不當成金額或日期", () => {
    const text = "依第3條辦理，文號字第115號。";
    expect(kinds(text)).toEqual([]);
    expect(flags(text, "請依第3條辦理，文號仍為字第115號。")).toEqual([]);
  });

  it("改寫拿掉的金額列在缺少清單，改寫欄沒有紅字", () => {
    const compared = compareLetterRewrite("費用新臺幣1,200元。", "費用另議。");
    expect(compared.changed).toEqual([]);
    expect(compared.missingLabels).toEqual(["新臺幣1,200元"]);
  });

  it("相對期限改了要標，時刻寫法相同不標", () => {
    expect(flags("請於下週五前回覆。", "請於下星期五前回覆。")).toEqual([]);
    expect(flags("請於下週五前回覆。", "請於下週一前回覆。")).toEqual([
      { kind: "deadline", raw: "下週一前" },
    ]);
    expect(flags("下午3時開始。", "下午三時開始。")).toEqual([]);
    expect(flags("下午3時開始。", "下午4時開始。")).toEqual([
      { kind: "deadline", raw: "下午4時" },
    ]);
  });

  it("模型聲稱沒改，程式仍標出被改的日期", () => {
    const compared = compareLetterRewrite(
      "3月2日辦理，費用1,200元。",
      "日期沒有改。改為3月9日辦理，費用1,200元。",
    );
    expect(compared.changed.map((fact) => fact.raw)).toEqual(["3月9日"]);
    expect(compared.segments.some((segment) => segment.flag === "date" && segment.text === "3月9日")).toBe(true);
    expect(compared.segments.some((segment) => segment.flag && segment.text.includes("1,200"))).toBe(false);
  });
});
