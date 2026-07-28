// 外流面判定的純函式測試 —— 補救計畫 W1-1 ②③④⑥(前端側)。
//
// 淨退化陷阱(計畫的風險欄點名)
// ------------------------------
// 五級是 `無機密 < 營業秘密 < 機密 < 極機密 < 絕對機密`,而 legacy 的
// `classified` boolean 的鏡射規則是 **`classified = level >= 機密`**
// (`services/csp/app/api/conversations.py:125` 自己寫明)→ **營業秘密的
// `classified` 是 False**。所以「吃 boolean」等於「營業秘密＝無機密」。
//
// 門檻必須是「> 無機密」。寫成「>= 機密」或「絕對機密」就是重演第一輪的錯誤。
// **因此下面每一條都以「營業秘密」當案例釘死** —— 用絕對機密測會兩種寫法都綠。
//
// 列印遮蔽(⑤)刻意是**另一個**門檻:`>= 機密`。規格就是這樣寫的,不要為了
// 「一致」把兩個門檻併成一個。

import { describe, it, expect } from "vitest";
import {
  CLASSIFICATION_FLOOR,
  CLASSIFICATION_LEVELS,
  controlledLevelLabel,
  controlledActionNotice,
  isControlled,
  isPrintMasked,
} from "../runtime/classified.js";
import {
  EXPORT_SOURCE_SYSTEM,
  buildExportHeader,
  taipeiIso,
  taipeiStamp,
} from "../runtime/exportHeader.js";

const CONTROLLED = ["營業秘密", "機密", "極機密", "絕對機密"];

describe("isControlled — 五個外流面的單一判定來源(前端側)", () => {
  it("營業秘密就是受控(第一輪把它當無機密,這條是回歸鎖)", () => {
    expect(isControlled({ classificationLevel: "營業秘密" })).toBe(true);
    // legacy boolean 為 False 也不得放行 —— 這正是缺陷本體
    expect(isControlled({ classificationLevel: "營業秘密", classified: false })).toBe(true);
  });

  it("四個受控等級全部受控", () => {
    for (const level of CONTROLLED) {
      expect(isControlled({ classificationLevel: level })).toBe(true);
      expect(isControlled({ classification_level: level })).toBe(true);
    }
  });

  it("無機密不受控(不得誤擋)", () => {
    expect(isControlled({ classificationLevel: CLASSIFICATION_FLOOR })).toBe(false);
    expect(isControlled({ classification_level: "無機密", classified: false })).toBe(false);
  });

  it("未知/損壞的密等 fail-closed 視同受控", () => {
    for (const bad of ["絕密", "top secret", "", "   ", 3, {}, true]) {
      expect(isControlled({ classificationLevel: bad })).toBe(true);
    }
  });

  it("欄位缺漏時回退 legacy boolean latch(不是 fail-closed 全擋)", () => {
    // 本地新建、尚未與後端同步的對話沒有 classification_level;全擋會讓
    // 每一個新對話都不能複製 —— 那是誤擋,不是安全。
    expect(isControlled({})).toBe(false);
    expect(isControlled(null)).toBe(false);
    expect(isControlled(undefined)).toBe(false);
    expect(isControlled({ classified: true })).toBe(true);
  });

  it("boolean latch 為真時,即使密等寫著無機密也不得放行(單向閂鎖)", () => {
    expect(isControlled({ classificationLevel: "無機密", classified: true })).toBe(true);
  });
});

describe("isPrintMasked — 列印遮蔽刻意用另一個門檻(>= 機密)", () => {
  it("機密以上遮蔽", () => {
    for (const level of ["機密", "極機密", "絕對機密"]) {
      expect(isPrintMasked({ classificationLevel: level })).toBe(true);
    }
  });

  it("營業秘密不遮蔽內文(它受複製/匯出/分享管制,但列印遮蔽門檻更高)", () => {
    expect(isPrintMasked({ classificationLevel: "營業秘密" })).toBe(false);
    // 但它仍然受控 —— 兩個判定不同,這條就是在證明沒有被併成一個
    expect(isControlled({ classificationLevel: "營業秘密" })).toBe(true);
  });

  it("無機密不遮蔽;未知值 fail-closed 遮蔽", () => {
    expect(isPrintMasked({ classificationLevel: "無機密" })).toBe(false);
    expect(isPrintMasked({ classificationLevel: "亂碼等級" })).toBe(true);
  });

  it("欄位缺漏時回退 boolean latch(= r1_0003 backfill 的 floor 機密)", () => {
    expect(isPrintMasked({})).toBe(false);
    expect(isPrintMasked({ classified: true })).toBe(true);
  });
});

describe("controlledLevelLabel — 文案不得把營業秘密叫做「機密」", () => {
  it("回傳真實密等", () => {
    expect(controlledLevelLabel({ classificationLevel: "營業秘密" })).toBe("營業秘密");
    expect(controlledLevelLabel({ classificationLevel: "極機密" })).toBe("極機密");
  });

  it("無機密沒有標籤", () => {
    expect(controlledLevelLabel({ classificationLevel: "無機密" })).toBeNull();
    expect(controlledLevelLabel({})).toBeNull();
  });

  it("未知值標成「未知(視同受控)」,與後端 controlled_level_label 同語", () => {
    expect(controlledLevelLabel({ classificationLevel: "亂碼" })).toBe("未知(視同受控)");
  });

  it("只有 boolean latch 時回退 floor 機密(與浮水印一致)", () => {
    expect(controlledLevelLabel({ classified: true })).toBe("機密");
  });
});

describe("controlledActionNotice — N-3 禁令姿態 + N-4 收緊文案", () => {
  const conv = { classificationLevel: "營業秘密" };

  it("受控時回 blocked=true,並帶真實密等(不得寫成「機密對話」)", () => {
    const notice = controlledActionNotice(conv, "匯出");
    expect(notice.blocked).toBe(true);
    expect(notice.level).toBe("營業秘密");
    expect(notice.tooltip).toContain("營業秘密");
    expect(notice.tooltip).not.toContain("機密對話禁止");
  });

  it("tooltip 必含「依據」與「替代路徑」(N-3:不再整條消失)", () => {
    const { tooltip } = controlledActionNotice(conv, "複製");
    expect(tooltip).toContain("依據");
    expect(tooltip).toContain("替代路徑");
  });

  it("tooltip 必回答「為何昨天能匯出今天不行」(N-4)", () => {
    const { tooltip } = controlledActionNotice(conv, "匯出");
    expect(tooltip).toContain("昨天");
    expect(tooltip).toMatch(/只升不降|單向/);
  });

  it("無機密時 blocked=false,tooltip 是原本的動作名", () => {
    const notice = controlledActionNotice({ classificationLevel: "無機密" }, "匯出");
    expect(notice.blocked).toBe(false);
    expect(notice.tooltip).toBe("匯出");
  });

  it("四個受控等級都擋,且各自顯示自己的密等", () => {
    for (const level of CONTROLLED) {
      const notice = controlledActionNotice({ classificationLevel: level }, "分享");
      expect(notice.blocked).toBe(true);
      expect(notice.tooltip).toContain(level);
    }
  });
});

describe("buildExportHeader — ③ 密等 + 匯出者 + 時間 + 來源系統", () => {
  const when = new Date("2026-07-26T02:34:05Z"); // = 台北 10:34:05

  it("時間用 Asia/Taipei(UTC+8)", () => {
    expect(taipeiIso(when)).toBe("2026-07-26T10:34:05+08:00");
    expect(taipeiStamp(when)).toContain("2026-07-26 10:34:05");
    expect(taipeiStamp(when)).toContain("UTC+8");
  });

  it("四個欄位都在,markdown 與 json 兩種格式都有", () => {
    const header = buildExportHeader({
      level: "營業秘密",
      exporter: "alice",
      exportedAt: when,
    });
    for (const needle of ["密等：營業秘密", "匯出者：alice", "匯出時間：", "來源系統："]) {
      expect(header.markdown).toContain(needle);
      expect(header.json.notice.join("\n")).toContain(needle);
    }
    expect(header.json.classification_level).toBe("營業秘密");
    expect(header.json.exported_by).toBe("alice");
    expect(header.json.exported_at).toBe("2026-07-26T10:34:05+08:00");
    expect(header.json.source_system).toBe(EXPORT_SOURCE_SYSTEM);
  });

  it("缺密等時標成無機密而不是留空(檔案上永遠有密等)", () => {
    const header = buildExportHeader({ exporter: "bob", exportedAt: when });
    expect(header.markdown).toContain(`密等：${CLASSIFICATION_FLOOR}`);
  });

  it("缺匯出者時標成「未知」而不是留空", () => {
    const header = buildExportHeader({ level: "無機密", exportedAt: when });
    expect(header.markdown).toContain("匯出者：未知");
  });

  it("CLASSIFICATION_LEVELS 順序不可變(門檻靠它算)", () => {
    expect(CLASSIFICATION_LEVELS).toEqual([
      "無機密", "營業秘密", "機密", "極機密", "絕對機密",
    ]);
  });
});
