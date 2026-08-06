import { describe, it, expect, afterEach, vi } from "vitest";
import React from "react";
import { render, screen as rtlScreen } from "@testing-library/react";

import {
  BLOCKING_FAMILIES,
  DETECTOR_PATTERN_META,
  blockingHits,
  detectPII,
} from "../data.jsx";
import { RedactionHint } from "../trust.jsx";
import {
  mountOrchestrator,
  createFakeBackend,
  sendText,
  screen,
  waitFor,
} from "./helpers/orchestrator.jsx";

// 這一組測試守兩件事,而且兩件事互相拉扯:
//
//   1. **偵測器說「疑似有東西」的時候,通常真的有。** 改這一包之前,十個
//      院內樣本(採購案號、料號、財產編號、預算欄、年度欄、16 位序號、
//      公文編號、貼上來的套件版本…)十個全部命中。一條每天誤報的橫幅,
//      使用者兩週內就學會不看它 —— 然後真的有個資的那一天它就白掛了。
//   2. **它現在也認得憑證,但憑證永遠不擋人。** 擁有者要的是
//      「偵測 > 警示使用者不要輕易交出,就像我貼 apikey 給你一樣」:
//      認出來、講一句、讓人自己決定。
//
// ⚠ 這裡每一個「憑證」樣本都是**現編的假字串**(通篇 EXAMPLE / NOTAREAL)。
// 測試檔案裡不放真的憑證,過期的也不放 —— 這個 repo 是 PUBLIC。

// ---- 語料 ------------------------------------------------------------------

// 院內真的會有人貼進來、而且**不可以**觸發警示的東西。
// 每一條後面那句話是「為什麼舊版會誤報」。
const INSTITUTE_SAMPLES = [
  ["採購案號", "本案採購案號 A987654321，請依規定辦理。"],           // 舊:一個大寫字母 + 九碼 = 身分證
  ["料號", "料號 B234567890 已無庫存。"],                            // 同上
  ["財產編號", "財產編號 C112233445 移交清冊。"],                    // 同上
  ["公文編號（連字號）", "來文字號 1130-4567-8901-2345 請查照。"],    // 舊:四組四碼 = 信用卡
  ["預算欄（tab 貼上）", "設備費\t2000\t1000\t1000\t1000"],          // 舊:分隔符含 \t
  ["預算欄（換行貼上）", "1234\n5678\n9012\n3456"],                  // 舊:分隔符含 \n
  ["年度欄", "2021 2022 2023 2024"],                                 // 舊:沒有 Luhn
  ["16 位序號", "序號 1000 2000 3000 4000"],                         // 舊:沒有首碼限制
  ["套件版本", 'import ReactDOM from "react-dom@18.3.1";'],          // 舊:頂級網域可以是數字
  ["套件版本（scoped）", "npm i @vitejs/plugin-react@4.4.1"],        // 同上
  // 以下三條舊版就沒有誤報,列進來是**防止這一包把它們變成誤報** ——
  // 新加的憑證 pattern 最可能踩到的就是貼上來的程式碼。
  ["型別定義", "interface Login { password: string; }"],
  ["HF 變數名", "hf_hub_download_kwargs = {}"],
  // 以下五條是**驗收自己的語料**抓到、我原本漏掉的。留在這裡的理由不是
  // 「補一個洞」,是它們各自代表一種每天都會被貼進來的東西:
  // CSS class、SQL schema、驗證器、環境變數內插。
  ["SpinKit CSS class", "<div class='sk-fading-circle-container-wrapper'>"],
  ["SpinKit CSS class（更長）", "sk-three-bounce-container-wrapper-outer"],
  ["SQL schema", "password: varchar(255)"],
  ["Zod 驗證器", "const schema = { password: z.string().min(8) }"],
  ["環境變數內插", "PASSWORD=${DB_PASSWORD}"],
  ["環境變數內插（中文）", "密碼：${DB_PASSWORD}"],
  // 反向語料:通用「夠長又夠亂」的東西一律不算。任何一條 pattern 被放寬到
  // 開始吃這些形狀,下面那條測試就會紅。
  ["UUID", "id = 3f2504e0-4f89-11d3-9a0c-0305e82c3301"],
  ["base64url", "dGhpcy1pcy1qdXN0LWEtYmFzZTY0dXJsLXN0cmluZy1ub3QtYS1zZWNyZXQ"],
  ["長 hex", "9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c5b4a39281706f5e4d3c2b1a0"],
  ["隨機識別字", "const requestCorrelationIdentifier = 'a7Kd93LmQpXz20Rb'"],
  ["base64 資料", "blob = 'TmV2ZXIgZ29ubmEgZ2l2ZSB5b3UgdXAgb3IgbGV0IHlvdSBkb3du'"],
  ["sha256 雜湊", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"],
];

// 真的個資 —— 每一種至少兩筆,一筆過了不代表這條 pattern 還活著。
const REAL_PII_SAMPLES = [
  ["身分證", "他的身分證是 A123456789。", "id"],
  ["身分證（另一筆）", "F131234569", "id"],
  ["新式統一證號（第二碼 8）", "居留證號 A812345671 已登錄。", "id"],
  ["手機", "聯絡電話 0912-345-678。", "phone"],
  ["手機（無分隔）", "0987654321 打不通。", "phone"],
  ["承辦人 Email", "承辦人 wang.ming@ncsist.org.tw", "email"],
  ["Email（另一筆）", "chen@mail.ncsist.org.tw 收", "email"],
  ["信用卡（空白分隔）", "卡號 4111 1111 1111 1111", "card"],
  ["信用卡（連字號）", "卡號 5500-0000-0000-0004", "card"],
];

// 憑證。全部是現編的假值。
const CREDENTIAL_SAMPLES = [
  ["前綴式 API 金鑰", "key = sk-EXAMPLENOTAREALKEY000000000000000000", "api_key"],
  ["GitHub 式 token", "ghp_EXAMPLENOTAREALTOKEN000000000000", "api_key"],
  ["AWS 式 access key id", "AKIAEXAMPLENOTREAL00", "api_key"],
  ["sk-proj- 式金鑰", "sk-proj-EXAMPLE_NOT-A-REAL-KEY0000000000000000000000000000000", "api_key"],
  ["sk-ant-api03- 式金鑰", "sk-ant-api03-EXAMPLENOTAREAL000000000000000000000000000000000000", "api_key"],
  ["Bearer 標頭", "Authorization: Bearer EXAMPLE-NOT-A-REAL-TOKEN-000", "token"],
  ["JWT 三段結構", "eyJEXAMPLEHEADER.eyJEXAMPLEPAYLOAD.EXAMPLESIGNATURE", "token"],
  ["私密金鑰區塊", "-----BEGIN RSA PRIVATE KEY-----", "private_key"],
  ["密碼賦值", "password=EXAMPLE-not-real-9999", "password"],
  ["中文密碼賦值", "密碼：EXAMPLE-not-real-9999", "password"],
];

const CRED_DRAFT = "麻煩幫我看這段設定：ANILA_MODEL_API_KEY=sk-EXAMPLENOTAREALKEY000000000000000000";
const ID_NUMBER = "A123456789";

describe("草稿偵測器:誤報收斂", () => {
  it.each(INSTITUTE_SAMPLES)("%s 不再觸發警示", (_name, text) => {
    expect(detectPII(text)).toEqual([]);
  });

  it("整份院內語料的誤報數是 0（改這一包之前是 10/14）", () => {
    const firing = INSTITUTE_SAMPLES.filter(([, text]) => detectPII(text).length > 0);
    expect(firing.map(([name]) => name)).toEqual([]);
  });

  it.each(REAL_PII_SAMPLES)("%s 還是抓得到", (_name, text, kind) => {
    expect(detectPII(text).map((h) => h.kind)).toContain(kind);
  });

  it("身分證:檢查碼對的抓得到,只是長得像的不抓", () => {
    // 兩個字串的**形狀一模一樣**(一個大寫字母 + 九碼),差別只在檢查碼。
    // 這正是舊 pattern 分不出來的那一格。
    expect(detectPII(`身分證 ${ID_NUMBER}`).map((h) => h.kind)).toEqual(["id"]);
    expect(detectPII("採購案號 A987654321")).toEqual([]);
  });

  it("身分證:第二碼不是 1/2/8/9 的就不是身分證,即使湊巧過了檢查碼", () => {
    // `A012345675` 的檢查碼是對的 —— 只有第二碼(本國人性別碼 1/2、新式
    // 統一證號 8/9)說得出它不是一個身分證號。少了這一碼的限制,一批
    // 「檢查碼湊巧對了」的院內編號會直接變成疑似個資。
    expect(detectPII("編號 A012345675")).toEqual([]);
    expect(detectPII("編號 A712345679")).toEqual([]);
  });

  it("信用卡:Luhn 過得了的抓得到,預算表那一列不抓", () => {
    expect(detectPII("卡號 4111 1111 1111 1111").map((h) => h.kind)).toEqual(["card"]);
    // 同樣是四欄四碼,而且這一列連 Luhn 都過得了 —— 擋住它的是「分隔符不含 \t」。
    expect(detectPII("設備費\t2000\t1000\t1000\t1000")).toEqual([]);
    // 這一列 Luhn 過不了。
    expect(detectPII("2021 2022 2023 2024")).toEqual([]);
  });

  it("Email:人的信箱抓得到,套件版本不抓", () => {
    expect(detectPII("承辦人 wang.ming@ncsist.org.tw").map((h) => h.kind)).toEqual(["email"]);
    expect(detectPII("react-dom@18.3.1")).toEqual([]);
  });

  it("電話這一條沒有動:兩種寫法都還在", () => {
    expect(detectPII("0912-345-678").map((h) => h.kind)).toEqual(["phone"]);
    expect(detectPII("0987654321").map((h) => h.kind)).toEqual(["phone"]);
  });
});

describe("草稿偵測器:憑證", () => {
  it.each(CREDENTIAL_SAMPLES)("%s 認得出來", (_name, text, kind) => {
    expect(detectPII(text).map((h) => h.kind)).toContain(kind);
  });

  it("認出來的憑證會被說成人話,不是「偵測到敏感資訊」", () => {
    const labels = DETECTOR_PATTERN_META.filter((p) => p.family === "credential").map(
      (p) => p.label,
    );
    expect(labels).toEqual(["API 金鑰", "存取權杖", "私密金鑰", "密碼"]);
  });

  it("`sk-` 這一條要遵守它自己註解寫的規則:短前綴必須綁死長度", () => {
    // 驗收實測的那個字串。SpinKit 的 CSS class,貼一段前端程式碼就會有。
    expect(detectPII("sk-fading-circle-container-wrapper")).toEqual([]);
    // 真的金鑰(現編的假值)照樣認得 —— 三種形狀。
    expect(detectPII("sk-EXAMPLENOTAREALKEY000000000000000000").map((h) => h.kind)).toEqual(["api_key"]);
    expect(detectPII("sk-proj-EXAMPLE_NOT-A-REAL-KEY0000000000000000000000000000000").map((h) => h.kind)).toEqual(["api_key"]);
    expect(detectPII("sk-ant-api03-EXAMPLENOTAREAL000000000000000000000000000000000000").map((h) => h.kind)).toEqual(["api_key"]);
  });

  it("`password:` 後面要像一個祕密,不能像一行程式碼", () => {
    // 三條都是驗收實測命中的。schema、驗證器、環境變數內插 —— 這個院裡每天貼。
    expect(detectPII("password: varchar(255)")).toEqual([]);
    expect(detectPII("password: z.string().min(8)")).toEqual([]);
    expect(detectPII("PASSWORD=${DB_PASSWORD}")).toEqual([]);
    // 真的賦值照樣認得。
    expect(detectPII("password=EXAMPLE-not-real-9999").map((h) => h.kind)).toEqual(["password"]);
    expect(detectPII("密碼：EXAMPLE-not-real-9999").map((h) => h.kind)).toEqual(["password"]);
  });

  it("不做熵值判斷:貼上來的 base64 與雜湊不算憑證", () => {
    // 這是刻意留的洞。長度加亂度的判準在這裡等於對每一段貼上來的程式碼開火,
    // 而那是這個平台最常見的用法 —— 要不要做是擁有者的決定,不是偵測器的。
    expect(detectPII("TmV2ZXIgZ29ubmEgZ2l2ZSB5b3UgdXAgb3IgbGV0IHlvdSBkb3du")).toEqual([]);
    expect(
      detectPII("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
    ).toEqual([]);
  });
});

describe("憑證只警示,永遠進不了阻擋決定", () => {
  it("阻擋白名單裡沒有 credential —— 而且這是白名單,不是黑名單", () => {
    expect([...BLOCKING_FAMILIES]).toEqual(["pii"]);
    expect(BLOCKING_FAMILIES.includes("credential")).toBe(false);
  });

  it("白名單真的動不了 —— 凍結不能只是看起來凍結", () => {
    // ⚠ 這條測試是被驗收抓出來的:上一版寫 `Object.freeze(new Set(["pii"]))`,
    // `Object.isFrozen()` 回 true,而 `.add("credential")` 照樣成功。
    // 凍結 Set 是一個什麼都沒做的動作,而註解卻拿它當保證。
    expect(Object.isFrozen(BLOCKING_FAMILIES)).toBe(true);
    expect(() => BLOCKING_FAMILIES.push("credential")).toThrow();
    try {
      BLOCKING_FAMILIES[0] = "credential";
    } catch {
      /* 嚴格模式丟例外,寬鬆模式靜默失敗 —— 兩種都可以,重點是內容沒變 */
    }
    expect([...BLOCKING_FAMILIES]).toEqual(["pii"]);
    // 而且就算有人繞過凍結,擋人與否仍然由 filter 決定 —— 這才是承重的那一層。
    expect(blockingHits([{ kind: "api_key", label: "API 金鑰", family: "credential" }])).toEqual([]);
  });

  it("清單上每一條 credential pattern 都擋不了人（對整份清單閉合,不是逐條列舉）", () => {
    const creds = DETECTOR_PATTERN_META.filter((p) => p.family === "credential");
    expect(creds.length).toBeGreaterThan(0);
    for (const p of creds) {
      expect(blockingHits([{ kind: p.kind, label: p.label, family: p.family }])).toEqual([]);
    }
    // 而且每一條 pattern 都得表態自己是哪一族 —— 沒有 family 的 pattern
    // 會被下面那條「不認得就不擋」吃掉,悄悄地變成永遠不擋。
    for (const p of DETECTOR_PATTERN_META) {
      expect(["pii", "credential"]).toContain(p.family);
    }
  });

  it("不認得的 family 一律不擋（新增一族的預設方向是放行,不是擋人）", () => {
    expect(blockingHits([{ kind: "x", label: "X", family: "something-new" }])).toEqual([]);
    expect(blockingHits([{ kind: "y", label: "Y" }])).toEqual([]);
  });

  it("每一個憑證樣本走完整條路都是不可阻擋的", () => {
    for (const [, text] of CREDENTIAL_SAMPLES) {
      const hits = detectPII(text);
      expect(hits.length).toBeGreaterThan(0);
      expect(blockingHits(hits)).toEqual([]);
    }
  });

  it("混在一起的時候,個資照擋,而擋的理由只講個資", () => {
    const hits = detectPII(`${ID_NUMBER} 與 sk-EXAMPLENOTAREALKEY000000000000000000`);
    expect(hits.map((h) => h.kind).sort()).toEqual(["api_key", "id"]);
    expect(blockingHits(hits).map((h) => h.kind)).toEqual(["id"]);
  });
});

describe("憑證在掛起來的 shell 裡:提醒得到,但擋不住", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("模式選 block、草稿裡只有一把 API 金鑰 —— 訊息照樣送得出去", async () => {
    // 這是這一包最重要的一條。誤擋一段貼上來的程式碼,比漏提醒還貴:
    // 使用者當場就把模式切回 warn,然後這個功能對他而言永遠不存在了。
    const backend = createFakeBackend({ uiSettings: { redactionMode: "block" } });
    await mountOrchestrator({ backend });

    await sendText(CRED_DRAFT);

    await waitFor(() => {
      expect(backend.chatPayloads.length).toBe(1);
    });
    expect(JSON.stringify(backend.chatPayloads)).toContain("sk-EXAMPLENOTAREALKEY000000000000000000");
  });

  it("同一個 block 模式下,個資照樣擋得住（對照組:上面那條不是因為閘門壞了）", async () => {
    const backend = createFakeBackend({ uiSettings: { redactionMode: "block" } });
    await mountOrchestrator({ backend });

    await sendText(`我的身分證是 ${ID_NUMBER}`);

    expect(backend.chatPayloads).toHaveLength(0);
    expect(screen.getByRole("alert")).toBeTruthy();
  });

  it("被擋的時候,那句話只點名擋得住的那一種,不把憑證算進去", async () => {
    const backend = createFakeBackend({ uiSettings: { redactionMode: "block" } });
    await mountOrchestrator({ backend });

    await sendText(`${ID_NUMBER} 與 sk-EXAMPLENOTAREALKEY000000000000000000`);

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("身分證");
    // 講出一個「其實沒有擋」的理由,使用者會去刪那把金鑰,然後發現照樣被擋。
    expect(alert.textContent).not.toContain("API 金鑰");
  });
});

describe("提示列:說了會發生的事就要真的發生", () => {
  it("block 模式 + 只有憑證:不可以說「這則不會送出」", () => {
    const hits = detectPII(CRED_DRAFT);
    render(<RedactionHint hits={hits} mode="block" onChangeMode={() => {}} />);
    const bar = rtlScreen.getByText(/這則草稿裡疑似有/);
    expect(bar.textContent).toContain("API 金鑰");
    expect(bar.textContent).not.toContain("這則不會送出");
    expect(bar.textContent).toContain("只提醒");
  });

  it("block 模式 + 有個資:照舊說「這則不會送出」", () => {
    const hits = detectPII(`身分證 ${ID_NUMBER}`);
    render(<RedactionHint hits={hits} mode="block" onChangeMode={() => {}} />);
    expect(rtlScreen.getByText(/這則草稿裡疑似有/).textContent).toContain("這則不會送出");
  });
});
