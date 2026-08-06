import { describe, it, expect, afterEach, vi } from "vitest";
import React from "react";

import { mountOrchestrator, screen, fireEvent, act } from "./helpers/orchestrator.jsx";

// 設定 →「隱私 / 信任」分頁裡「敏感資訊處理」那段說明。
//
// 這個畫面的工作,是讓一個正在打字的人知道**送出去之後會發生什麼**,然後自己
// 決定要不要送。所以它要說的是兩件事:
//
//   1. 平台不會替你改動你打的字 —— 送給模型的、以及存下來的對話紀錄,都是原文。
//   2. 這個模式是你自己的偏好,沒有管理員替你設定過,而且你隨時可以改回來。
//
// ⚠ 這裡曾經寫「實際遮罩在 CSP proxy 層執行。UI 只在送出前提示;無法關閉後端的
// 審計與遮罩」。那是假的:CSP 後端沒有任何個資處理 —— 沒有模型、沒有欄位、
// 沒有 migration、沒有管理 API,也沒有治理畫面。在四級密等的平台上,對著正在
// 輸入身分證號的使用者斷言「伺服器會先把它拿掉」,而事實相反,正是
// 「讓使用者以為發生了什麼、其實什麼也沒發生」的那一類控制項。
//
// 現在連前端都不再改動任何東西,所以這個畫面上**任何**「平台會替你拿掉／
// 遮起來／去識別」的說法都是謊。下面第一組測試同時釘正面主張(原文、完整)
// 與反面(那幾個詞不准出現)。
describe("設定 → 隱私 / 信任:敏感資訊處理的說明", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  // 掛的是**真的** App,然後照使用者的走法點進去:點右上角「設定」→ 切到
  // 「隱私 / 信任」分頁。所以這段文案不只是「存在於某個元件裡」,而是真的
  // 從使用者走得到的路徑上讀得到。
  const openPrivacyBlurb = async () => {
    await mountOrchestrator();
    await act(async () => {
      fireEvent.click(screen.getByTitle("設定"));
    });
    await act(async () => {
      fireEvent.click(screen.getByText("隱私 / 信任").closest("button"));
    });
    // 錨點取那個不會變的小標,再讀它同一個區塊 —— 這樣斷言不依賴說明本身
    // 用了哪個詞,文案被換成另一句謊也照樣抓得到(而不是查無此元素)。
    return screen.getByText("敏感資訊處理").parentElement.textContent;
  };

  // ⚠ 底下釘的是**主張**,不是措辭。
  //
  // 第一版用 `toContain("模型")` 這種字面比對,結果把「模型」改寫成同樣誠實的
  // 「LLM」就會紅 —— 一個在產品誠實度毫無變化時卻擋著人的測試,下一個趕進度的
  // 人會直接刪掉它,而且他是對的。所以每一條都放成同義詞的交集(regex),
  // 只要那個**主張**還在就綠。
  //
  // ⚠⚠ 這組測試守得住什麼、守不住什麼,請照這個講法理解:
  //
  //   守得住 **取代式**的漂移 —— 把誠實的句子改寫成謊話。必須在場的那幾個
  //   主張(原文、完整、沒有管理員設定)會跟著不見,於是變紅。
  //
  //   守不住 **追加式**的謊 —— 在下面**多加一句**「為降低外洩風險,平台會在
  //   傳送前自動去除已標記的個資欄位」。原本的句子一句沒動,每一條斷言照樣
  //   通過,全套測試全綠,而這個畫面已經自相矛盾了。實測過,確實是綠的。
  //
  // 下面那條反向斷言(不准出現「遮罩／遮蔽／去識別」)會擋掉最常見的幾種寫法,
  // 但它**不是**一份能窮舉的清單 —— 中文裡表達「伺服器會幫你拿掉」的句型永遠
  // 補不完,這個專案已經被這個形狀咬過(見 CLAUDE.md 的黑名單那條教訓)。
  // 真正擋得住追加式謊話的是人:文案審查,以及「這個畫面上的每一句話都要
  // 有人能指出它對應到哪一行程式」這個要求。
  //
  // 所以:**這裡綠燈只代表沒有人改壞既有的句子,不代表這個畫面上每一句話
  // 都是真的。** 新增文案時不要以為跑過測試就算數。

  it("不可以宣稱平台會替使用者拿掉／遮起來任何東西", async () => {
    const t = await openPrivacyBlurb();
    // 舊的那三句(便宜,留著當回歸釘)。
    expect(t).not.toContain("CSP proxy");
    expect(t).not.toContain("CSP 層");
    expect(t).not.toContain("後端的審計與遮罩");
    // 平台不再改動任何文字,所以這幾個詞在這個畫面上一律是謊。
    for (const word of ["遮罩", "遮蔽", "去識別", "自動移除"]) {
      expect(t).not.toContain(word);
    }
  });

  it("要講明送出去的是原文 —— 模型與存下來的對話紀錄收到的是完整文字", async () => {
    const t = await openPrivacyBlurb();
    expect(t).toMatch(/原文/);
    expect(t).toMatch(/模型|LLM/);
    expect(t).toMatch(/對話紀錄|對話記錄|存下來/);
    expect(t).toMatch(/完整|不多不少|一字不改/);
  });

  it("要講明決定權在使用者 —— 送出去就收不回來", async () => {
    const t = await openPrivacyBlurb();
    expect(t).toMatch(/收不回來|無法收回/);
    expect(t).toMatch(/由你決定|你決定|自己決定/);
  });

  // ⚠ 這一條是被實測抓出來的:這段文案原本寫「一個字不多不少」,而 `chat.jsx`
  // 的 submit 會 `text.trim()` —— 貼上來的程式碼前後空白**真的**會被去掉。
  // 一句「我們完全不動你的字」的承諾,只要有一個反例就是謊,而這個反例每天
  // 都在發生。要嘛不承諾,要嘛把那個例外講出來;這裡選後者。
  it("要講明唯一會被動到的東西:頭尾空白", async () => {
    const t = await openPrivacyBlurb();
    expect(t).toMatch(/頭尾|前後/);
    expect(t).toMatch(/空白/);
  });

  // ⚠ 偵測器只比對格式,實測七個院內樣本有六個是誤報。設定頁若不講,使用者
  // 第一次被誤擋時只會覺得平台壞了 —— 而他其實只要把模式切回 warn。
  it("要講明偵測會認錯 —— 不可以讓人以為平台真的認得個資", async () => {
    const t = await openPrivacyBlurb();
    expect(t).toMatch(/誤認|認錯|不保證|不一定/);
  });

  it("不可以宣稱是管理員決定的 —— 而且要講明沒有人替你設定過", async () => {
    const t = await openPrivacyBlurb();
    // ⚠ 這一條原本寫成 `not.toContain("管理員")`,那是**釘錯東西**:它禁的是
    // 一個詞,不是一句謊。結果為了讓它變綠,「這是你自己的偏好,不是管理員
    // 政策」這句**真話、而且有用**的說明被刪掉了 —— 而那正是一個被擋住、
    // 搞不清楚是誰擋的人最需要看到的一句。產品因為測試而變差了。
    //
    // 現在釘的是主張:這個畫面上唯一誠實的「管理員」句型是**否定句**
    // (後端根本沒有這種政策欄位)。要求那句否定必須在場 —— 想把它改寫成
    // 「管理員已設定為阻擋」的人,留不住它。
    expect(t).toMatch(/沒有管理員|不是管理員|沒有人替你設定/);
  });

  it("要講明這個選擇存在哪裡 —— 不然使用者不知道它跨不跨機器", async () => {
    const t = await openPrivacyBlurb();
    expect(t).toMatch(/帳號|卡/);
    expect(t).toMatch(/保留|存/);
  });

  // ⚠ 這一條守的不是文案,是**出路存不存在**。
  //
  // 在它之前,能改敏感資訊模式的地方只有輸入框上方那條提示列,而那條提示列
  // 只在草稿裡剛好偵測到個資時才出現。於是:一個幾週前在別台機器把模式設成
  // block 的人,今天被擋下來(輸入框可能還是空的),整個平台上找不到任何一個
  // 開關可以改回來。設定頁那時候一個控制項都沒有。
  //
  // 擋人的控制項一定要有一條當事人自己走得出去的路 —— 這是這個平台上一輪
  // 25 天工作被整包放棄的直接原因(限制越加越多、沒人想維護也沒人想用)。
  it("要有一組永遠到得了的模式開關,而且真的改得動", async () => {
    await openPrivacyBlurb();

    const blockBtn = screen.getByText("block");
    expect(blockBtn).toBeTruthy();
    expect(screen.getByText("warn")).toBeTruthy();
    // 模式只有兩個 —— 曾經有第三個(只改畫面顯示、不影響送出的那個),
    // 它已經被拿掉了,這裡不可以又冒出一顆按鈕來。
    expect(
      screen.queryAllByText(/^(mask|遮罩)$/).length,
    ).toBe(0);

    // 預設是 warn;按下 block 之後,按鈕要真的變成選中狀態。
    expect(blockBtn.getAttribute("aria-pressed")).toBe("false");
    expect(screen.getByText("warn").getAttribute("aria-pressed")).toBe("true");
    await act(async () => {
      fireEvent.click(blockBtn);
    });
    expect(screen.getByText("block").getAttribute("aria-pressed")).toBe("true");
  });
});
