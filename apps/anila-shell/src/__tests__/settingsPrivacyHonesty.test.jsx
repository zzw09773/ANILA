import { describe, it, expect, afterEach, vi } from "vitest";
import React from "react";

import { mountOrchestrator, screen, fireEvent, act } from "./helpers/orchestrator.jsx";

// 設定 →「隱私 / 信任」分頁裡「敏感資訊處理」那段說明。
//
// 這裡曾經寫「實際遮罩在 CSP proxy 層執行。UI 只在送出前提示;無法關閉後端的
// 審計與遮罩」。那是假的,而且是三處說法裡講得最明確的一處:
//
//   * CSP 後端沒有任何個資遮罩 —— 沒有模型、沒有欄位、沒有 migration、
//     沒有管理 API,也沒有治理畫面。沒有哪個管理員設定過這件事。
//   * 送出的 body 是原文(buildUserContent 原樣回傳 text → runtime/sse.js 的
//     fetch body),落庫的 content 也是原文(persistTurnHead)。
//   * `maskPII` 唯一的消費者是 data.jsx 的 renderWithRedaction —— 那是畫面渲染,
//     不在送出路徑上。
//
// 在四級密等的平台上,對著正在輸入身分證號的使用者斷言「伺服器會先把它拿掉」,
// 而事實相反 —— 這正是「讓使用者以為發生了什麼、其實什麼也沒發生」的那一類控制項。
// 2026-07-30 的兩輪誠實化各修掉一處(組字列的承諾、訊息泡泡上的斷言),
// 就是漏了這一處。這組測試是為了不讓它漏第三次。
//
// 真正要遮罩就得在送出前替換 body(那是功能,不是文案)。如果哪天真的做了
// 伺服器端遮罩,這組測試會擋住你 —— 那時請連同這段註解一起改。
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
  // 反向那條同理:黑名單三個舊字串永遠補不完(換個講法就繞過去了)。收斂的
  // 寫法是釘住「必須說出口的否定」——「沒有伺服器端的遮罩」一旦被改成謊,
  // 那句話就留不住。
  //
  // ⚠⚠ 這組測試守得住什麼、守不住什麼,請照這個講法理解:
  //
  //   守得住 **取代式**的漂移 —— 把誠實的句子改寫成謊話。必須在場的那幾個
  //   否定主張(沒有伺服器端遮罩／沒有管理員設定)會跟著不見,於是變紅。
  //
  //   守不住 **追加式**的謊 —— 在下面**多加一句**「為降低外洩風險,平台會在
  //   傳送前自動去除已標記的個資欄位」。原本的句子一句沒動,每一條斷言照樣
  //   通過,全套測試全綠,而這個畫面已經自相矛盾了。實測過,確實是綠的。
  //
  // 這個限制**不是**寫一份「禁止的說法」清單就能補起來的。那份清單要窮舉
  // 中文裡所有能表達「伺服器會幫你遮罩」的句型,而它永遠補不完 ——
  // 這個專案已經被這個形狀咬過(見 CLAUDE.md 的黑名單那條教訓)。
  // 真正擋得住追加式謊話的是人:文案審查,以及「這個畫面上的每一句話都要
  // 有人能指出它對應到哪一行程式」這個要求。
  //
  // 所以:**這裡綠燈只代表沒有人改壞既有的句子,不代表這個畫面上每一句話
  // 都是真的。** 新增文案時不要以為跑過測試就算數。

  it("不可以宣稱遮罩是在 CSP / 伺服器那一層做的", async () => {
    const t = await openPrivacyBlurb();
    // 舊的那三句(便宜,留著當回歸釘)。
    expect(t).not.toContain("CSP proxy");
    expect(t).not.toContain("CSP 層");
    expect(t).not.toContain("後端的審計與遮罩");
    // 收斂的那條:必須說出「沒有伺服器端遮罩」這個否定主張。
    expect(t).toMatch(/沒有(伺服器端|後端)/);
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

  it("要講明遮蔽只在本畫面,送出的是原文", async () => {
    const t = await openPrivacyBlurb();
    expect(t).toMatch(/本畫面|這個畫面|畫面上/);
    expect(t).toMatch(/原文|未經遮罩/);
  });

  it("要講明模型與存下來的對話紀錄收到的是完整文字", async () => {
    const t = await openPrivacyBlurb();
    expect(t).toMatch(/模型|LLM/);
    expect(t).toMatch(/對話紀錄|對話記錄|存下來/);
    expect(t).toMatch(/完整文字|完整內容|全文/);
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
    expect(screen.getByText("mask")).toBeTruthy();

    // 預設是 mask;按下 block 之後,按鈕要真的變成選中狀態。
    expect(blockBtn.getAttribute("aria-pressed")).toBe("false");
    await act(async () => {
      fireEvent.click(blockBtn);
    });
    expect(screen.getByText("block").getAttribute("aria-pressed")).toBe("true");
  });
});
