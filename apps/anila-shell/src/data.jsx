// Default folder seed + PII helpers — real agents load dynamically from CSP.

// Seed list. Users can add/remove folders at runtime (persisted in localStorage
// under "anila-folders"). `all` and `starred` are protected because the
// sidebar filter logic treats them specially (no-filter / starred-only).
//
// No demo folders are seeded beyond the two built-ins — a fresh account
// starts with a clean sidebar and the user builds their own taxonomy with
// "＋ 新增".
export const DEFAULT_FOLDERS = [
  { id: "all",     name: "全部",   icon: "inbox" },
  { id: "starred", name: "已加星", icon: "star" },
];

export const BUILTIN_FOLDER_IDS = new Set(["all", "starred"]);

// ---- Sensitive-string detection patterns ----
// Front-end only. The detector has exactly two consumers: the composer hint bar
// (`warn` — tell the user what is in the draft) and the send gate (`block` —
// refuse to send it). Neither of them alters the text: what the user typed is
// what the model receives and what the conversation record keeps, byte for byte.
//
// ⚠ **這份清單只認得它列出來的形狀,永遠不會是完整的。** 沒被列到的個資與
// 祕密照樣送得出去 —— 這裡做的是「認出來就提醒」,不是「保證攔得住」。
// 提示列的用字必須跟這件事一致:說「疑似」,不說「有」。
//
// 每一條 pattern 有兩個欄位決定它的下場:
//
//   * `family` — 決定它進不進得了**阻擋**決定(見下面的 BLOCKING_FAMILIES)。
//   * `validate` — 格式對了還要再驗一次的那些(身分證檢查碼、信用卡 Luhn)。
//     回 false 的命中直接丟掉,不會出現在提示列上。
//
// ⚠ 加新 pattern 的人請注意:**寧可漏也不要誤報**。這條橫幅只有在使用者
// 相信它的時候才有價值;對著一張預算表說「這裡有信用卡」,錯一次他就再也
// 不看了,而真的有東西的那一天它就白掛了。
const PII_PATTERNS = [
  {
    kind: "id",
    label: "身分證",
    family: "pii",
    // 首碼 A–Z(戶籍地),第二碼 1/2 是本國人性別碼、8/9 是 2021 年新式
    // 統一證號的外籍人士碼。舊的 `\b[A-Z]\d{9}\b` 沒有這兩個限制也沒有
    // 檢查碼,於是院內的採購案號、料號、財產編號全部命中。
    regex: /\b[A-Z][1289]\d{8}\b/g,
    validate: isTaiwanIdNumber,
  },
  {
    kind: "phone",
    label: "電話",
    family: "pii",
    // 未改動。它要求 `09` 開頭且剛好十碼(前後有 \b),本身已經夠窄 ——
    // 院內文件編號要撞上它得剛好是 09 開頭的十位數,實測語料裡一件也沒有。
    // 沒有理由就不動,是這一包的規則。
    regex: /\b09\d{2}-?\d{3}-?\d{3}\b/g,
  },
  {
    kind: "email",
    label: "Email",
    family: "pii",
    // 頂級網域限定字母且至少兩碼。舊的 `\.[\w.-]+` 收尾讓**貼進來的程式碼與
    // 套件版本**變成「Email」:`react-dom@18.3.1`、`@vitejs/plugin-react@4.4.1`
    // 都命中(`18` 當網域、`3.1` 當頂級網域)。這不是把 Email 的定義改窄,
    // 是原本那條根本沒在比對 Email。
    regex: /[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b/g,
  },
  {
    kind: "card",
    label: "信用卡",
    family: "pii",
    // 兩個修正:
    //   1. 分隔符從 `[-\s]` 收成 `[- ]`。`\s` 含換行與 tab,於是**任何從表格
    //      貼進來的四欄四位數**(預算表、年度欄、料號表)都是一張信用卡。
    //   2. 分隔符用 \1 回填,三個位置必須一致 —— 表格貼上常是混合空白。
    //   3. 首碼限 2–6:卡號第一碼是發卡產業別,消費卡只出現在這個範圍
    //      (Visa 4、MasterCard 5 與 2221–2720、JCB 35、Discover 6、銀聯 62)。
    //      院內的序號與預算欄常以 1 或 0 開頭,這一碼就把它們送走了。
    // 再加 Luhn 檢查碼(validate),把剩下的隨機十六位數擋掉九成。
    //
    // ⚠ 這三道加起來仍然不是「這是一張卡」的證明,只是把「長得像」收窄到
    // 「連檢查碼都對」。剩下的誤報還是有,只是變成少數。
    regex: /\b[2-6]\d{3}([- ]?)\d{4}\1\d{4}\1\d{4}\b/g,
    validate: passesLuhn,
  },

  // ── 憑證與金鑰(family: "credential",**只警示,永遠不阻擋**)────────────
  //
  // 平台擁有者要的就是這一格:「偵測 > 警示使用者不要輕易交出,就像我貼
  // apikey 給你一樣」。偵測、然後讓人自己決定。
  //
  // ⚠ 只認**有前綴或有結構**的形狀。不做「夠長又夠亂就是祕密」那種熵值判斷 ——
  // 在這裡那等於對每一段貼進來的 base64 或 hex 開火,而貼程式碼正是這個平台
  // 最常見的用法。
  {
    kind: "api_key",
    label: "API 金鑰",
    family: "credential",
    // 每一種都綁**該家自己的前綴與長度**,不是一條共用的「前綴＋夠長」。
    // 理由是誤報:`hf_` 那種短前綴一旦只要求「再 16 個字」,`hf_hub_download_kwargs`
    // 這種再普通不過的變數名就變成一把金鑰了,所以短前綴要嘛綁死長度、
    // 要嘛不收。GitHub 的 token 本體不含底線,`gh[pousr]_` 因此撞不到
    // `ghp_my_variable_name`。
    //
    // ⚠ `sk-` 這一條 2026-08-06 收緊過一次,而且是被自己上面這段話抓到的:
    // 原本寫 `sk-[A-Za-z0-9_-]{16,}` —— 三個字元的前綴配一個很鬆的下限,
    // 正是這段註解說不可以收的那一型。實測命中 `sk-fading-circle-container-wrapper`
    // (SpinKit 的 CSS class,貼一段前端程式碼就會有)。現在拆成兩種真實形狀:
    //   * 傳統金鑰:`sk-` ＋ 32 個以上**純英數**(連字號一出現就不是它)
    //   * 有子前綴的:`sk-proj-` / `sk-ant-api03-` ＋ 40 個以上
    // kebab-case 的識別字兩種都撞不到。
    regex: /\b(?:sk-(?:proj|ant-api\d{2})-[A-Za-z0-9_-]{40,}|sk-[A-Za-z0-9]{32,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}|xox[abporsu]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35})\b/g,
  },
  {
    kind: "token",
    label: "存取權杖",
    family: "credential",
    // 兩種:`Authorization: Bearer …` 這個講法本身,以及 JWT 的三段結構
    // (第一段一定是 `eyJ`,因為那是 `{"` 的 base64url)。兩者都是**形狀**,
    // 不是長度或亂度。
    regex: /\bBearer[ \t]+[A-Za-z0-9._~+/-]{12,}=*|\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b/g,
  },
  {
    kind: "private_key",
    label: "私密金鑰",
    family: "credential",
    regex: /-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----/g,
  },
  {
    kind: "password",
    label: "密碼",
    family: "credential",
    // 「password: 值」這種一眼就看得出來的賦值。分隔符後面那一段必須長得像
    // **一個真的祕密字面值**,而不是像一行程式碼:
    //   * 字元集不含 `$ ( ) { } , ;` —— 這一條就是全部的重點。schema 定義、
    //     驗證器、環境變數內插都靠這些字元活著,把它們排除掉,
    //     `password: varchar(255)`、`password: z.string().min(8)`、
    //     `PASSWORD=${DB_PASSWORD}` 三種都在第一個字元就出局。
    //   * 至少八個字元,而且**必須含一個數字**。`password: string`、
    //     `password = required` 因此不算。
    //
    // 代價寫在這裡:純字母的密碼(`password: MySecret`)偵測不到。這是刻意選的
    // 方向 —— 這條規則寧可漏,也不要每次有人貼一段型別定義就跳一次警示。
    // 一條每天喊狼來了的規則,不如沒有。
    //
    // ⚠ 沒有 `i` 旗標 —— 大小寫寫進字面裡。加了 `i`,底下任何一個
    // 「限定小寫」的判斷都會跟著失效。
    regex: /(?:[Pp]assword|PASSWORD|[Pp]asswd|[Pp]wd|密碼)[ \t]*[:=：][ \t]*["']?(?=[A-Za-z0-9._~+\/=!@#%^&*-]*[0-9])[A-Za-z0-9._~+\/=!@#%^&*-]{8,}/g,
  },
];

/**
 * 哪些 family 進得了**阻擋**決定。白名單,不是黑名單。
 *
 * ⚠ 這是「憑證只警示、永遠不阻擋」那條規則的**結構**,不是約定。名單沒列到的
 * family(包含以後新加的、以及忘了填 family 的)一律不阻擋 —— 也就是說,
 * 想讓某一類擋得住送出,得有人**刻意**把它加進這一行;而想讓它擋不住,
 * 什麼都不用做。方向是刻意選的。
 *
 * 理由:誤擋比漏擋貴。一個把貼上來的程式碼片段擋下來的偵測器,使用者第一天
 * 就會把模式切回 warn,然後這個功能對他而言就永遠不存在了。憑證的形狀
 * (`sk-…`、`Bearer …`、`password=…`)最常出現在**貼上來的程式碼**裡,
 * 正是誤擋成本最高的那一類。
 *
 * ⚠ 用凍結**陣列**而不是 `Object.freeze(new Set([...]))`:對 Set 呼叫
 * `Object.freeze` 是一個看起來有效、實際上什麼都沒做的動作 ——
 * `Object.isFrozen()` 回 true,而 `.add("credential")` 照樣成功
 * (Set 的內容不是自有屬性,凍結管不到它)。實測過。凍結陣列是真的:
 * `push` / 索引指派在 ESM 的嚴格模式下會丟 TypeError。
 *
 * ⚠ 但真正扛住這個不變式的仍然是下面那個 filter,不是凍結。凍結擋的是
 * 「有人不小心在別的地方加一族」,filter 決定的是「不在名單上就不擋人」。
 */
export const BLOCKING_FAMILIES = Object.freeze(["pii"]);

/**
 * 命中裡「可以拿來擋送出」的那些。送出閘門**只准**看這個函式的結果。
 *
 * @param {{family?: string}[]} hits
 * @returns {{family?: string}[]}
 */
export function blockingHits(hits) {
  if (!hits || hits.length === 0) return [];
  return hits.filter((h) => BLOCKING_FAMILIES.includes(h && h.family));
}

/**
 * 每條 pattern 的中繼資料(不含 regex,避免呼叫端動到共用的 lastIndex)。
 * 給測試用:讓「憑證不得進入阻擋」這件事可以對**整份清單**閉合地驗一次,
 * 而不是逐條列舉 —— 逐條列舉的守衛,下一個人加第五種憑證時就漏了。
 */
export const DETECTOR_PATTERN_META = Object.freeze(
  PII_PATTERNS.map((p) => Object.freeze({ kind: p.kind, label: p.label, family: p.family })),
);

// 內政部公告的身分證字號英文代碼(A=10 … Z=33)。
const TW_ID_LETTER_CODES = {
  A: 10, B: 11, C: 12, D: 13, E: 14, F: 15, G: 16, H: 17, I: 34,
  J: 18, K: 19, L: 20, M: 21, N: 22, O: 35, P: 23, Q: 24, R: 25,
  S: 26, T: 27, U: 28, V: 29, W: 32, X: 30, Y: 31, Z: 33,
};

/**
 * 身分證字號檢查碼。字母換成兩位數 n1n2,加權 1、9,其後八碼加權 8…1,
 * 最後一碼(檢查碼)加權 1,總和能被 10 整除才算數。
 *
 * 這是「這串字**是不是**一個身分證號」與「它長得像不像」的差別 —— 院內的
 * 採購案號 `A987654321` 長得一模一樣,而它過不了這一關。
 *
 * @param {string} value 已經通過格式比對的 10 字元字串。
 */
function isTaiwanIdNumber(value) {
  const code = TW_ID_LETTER_CODES[value[0]];
  if (code === undefined) return false;
  let sum = Math.floor(code / 10) + (code % 10) * 9;
  for (let i = 1; i <= 8; i += 1) sum += Number(value[i]) * (9 - i);
  sum += Number(value[9]);
  return sum % 10 === 0;
}

/**
 * Luhn 檢查碼 —— 全世界的信用卡號都帶著它。純粹十六位數字的隨機字串
 * (預算欄、料號、序號)有九成過不了。
 *
 * @param {string} value 可含 `-` 或空白分隔符。
 */
function passesLuhn(value) {
  const digits = value.replace(/[^0-9]/g, "");
  if (digits.length === 0) return false;
  let sum = 0;
  let double = false;
  for (let i = digits.length - 1; i >= 0; i -= 1) {
    let d = Number(digits[i]);
    if (double) {
      d *= 2;
      if (d > 9) d -= 9;
    }
    sum += d;
    double = !double;
  }
  return sum % 10 === 0;
}

export function detectPII(text) {
  if (!text) return [];
  const hits = [];
  PII_PATTERNS.forEach(p => {
    p.regex.lastIndex = 0;
    let m;
    while ((m = p.regex.exec(text)) !== null) {
      // 格式命中之後還有檢查碼那一關(身分證、信用卡)。過不了的就不是命中 ——
      // 它連提示列都不該出現,使用者看到的每一個計數都得是查過的。
      if (p.validate && !p.validate(m[0])) continue;
      hits.push({ kind: p.kind, label: p.label, family: p.family, value: m[0], index: m.index });
    }
  });
  return hits.sort((a, b) => a.index - b.index);
}

/**
 * 把偵測結果講成一句人話:`1 個身分證、2 個 Email`。
 *
 * 提示列與阻擋通知都用它。理由:「偵測到敏感資訊」只講了一個關於字串的事實,
 * 本來就知道那是什麼的人只是被拖了一秒,不知道的人什麼也沒學到。要讓人停下來
 * 想一下,得先說出**找到的是什麼**。
 *
 * @param {{label: string}[]} hits
 * @returns {string} 空陣列回空字串。
 */
export function summarizePIIHits(hits) {
  if (!hits || hits.length === 0) return "";
  const byLabel = new Map();
  hits.forEach((h) => byLabel.set(h.label, (byLabel.get(h.label) || 0) + 1));
  return [...byLabel].map(([label, n]) => `${n} 個${label}`).join("、");
}
