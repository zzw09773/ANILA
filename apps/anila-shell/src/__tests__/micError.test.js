import { describe, it, expect } from "vitest";
import { micErrorMessage } from "../asr/asrStream.js";

// 2026-07-31:擁有者按了麥克風,拿到 NotFoundError,而當時的訊息叫他去查
// 「權限 / https / 伺服器放行」—— 三條都對,但沒有一條是原因。真正的原因是
// 這台機器的音訊系統沒有掛出任何輸入裝置。這組測試釘住的是:
// **不同的失敗原因,必須指向不同的排錯方向。**
describe("micErrorMessage", () => {
  it("找不到裝置時,不可以叫使用者去檢查權限", () => {
    const msg = micErrorMessage({ name: "NotFoundError" });
    expect(msg).toContain("找不到");
    expect(msg).toContain("不是權限問題");
    // 這是重點:把人導向權限與網址,就是 07-31 那次浪費掉的時間。
    expect(msg).not.toContain("https");
    expect(msg).not.toContain("允許");
  });

  it("被瀏覽器擋下時,才提權限與 https,而且三種可能都要講", () => {
    const msg = micErrorMessage({ name: "NotAllowedError" });
    expect(msg).toContain("允許麥克風");
    expect(msg).toContain("https");
    expect(msg).toContain("localhost"); // 自簽憑證那一種,最容易被漏掉
    expect(msg).not.toContain("找不到");
  });

  it("裝置被別的程式佔用,是第三種方向", () => {
    const msg = micErrorMessage({ name: "NotReadableError" });
    expect(msg).toContain("佔用");
    expect(msg).not.toContain("允許麥克風");
    expect(msg).not.toContain("找不到");
  });

  it("沒見過的錯誤要把名字原樣吐出來,不要假裝知道原因", () => {
    expect(micErrorMessage({ name: "WeirdError" })).toContain("WeirdError");
    expect(micErrorMessage(undefined)).toContain("UnknownError");
  });

  it("三個方向兩兩不同 —— 否則等於沒有分辨", () => {
    const seen = new Set(
      ["NotFoundError", "NotAllowedError", "NotReadableError"].map((n) =>
        micErrorMessage({ name: n }),
      ),
    );
    expect(seen.size).toBe(3);
  });
});
