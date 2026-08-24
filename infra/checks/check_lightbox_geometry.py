#!/usr/bin/env python3
"""Check 4 · 燈箱／圖片渲染的**幾何**——jsdom 量不到的那一半。

為什麼要有這一支
----------------
`apps/anila-shell` 跑 vitest + jsdom，**jsdom 沒有版面引擎**
（`getBoundingClientRect()` 全回 0、`elementFromPoint` 不做真正的命中判定）。
`citedFigures.test.jsx` 對燈箱的斷言**全部是 style 字串**——守的是寫法，不是幾何。
2026-08-22 一天之內三條使用者可見的缺陷，**沒有一條是測試抓到的**：

  V-2  letterbox 產生 1535px 吞點擊死區（直式圖左右各約 767px 點了沒反應）
  R-1  修 V-2 的 pointer-events:none 把「圖上右鍵另存」整個封掉
  A6-1 修 R-1 後，onLoad 前那一幀以自然尺寸渲染，7.9 倍視窗寬、捲不動

⚠ **這支不會自己跑**（`run-all.sh` 是三人手動入口、不擋 commit／merge／deploy）。
它比一支孤兒腳本強的地方是「**休眠在一個有人會回去的地方**」，**不是「不再休眠」**。
驗收模板的那個問句才是觸發：**這個 diff 有沒有改到會被畫出來的東西？**

相依
----
**不新增任何 npm／pip 相依**：用系統上的 Chrome/Chromium，
`--headless --dump-dom` 把頁面內量到的數字讀回來。
🔴 **找不到瀏覽器時回 BROKEN(1)，不回 PASS**——
一支「沒有瀏覽器就安靜通過」的幾何檢查比沒有檢查更糟。

量測素材從原始碼抽出，不手抄
----------------------------
`fitLightboxBox` 的函式本體與 backdrop／img 的 style 物件都是從
`apps/anila-shell/src/markdown.jsx` **抽出來**再注入量測頁的，並且會印出抽到的東西。
手抄的複本會與被測物安靜漂開，而漂在哪裡沒有任何徵兆。

不在本支範圍
------------
`fitLightboxBox` 的**算術**（高圖夾高、寬圖夾寬）是純函式，jsdom 也算得出來，
**那一格屬於單元測試**，不要在這裡重複一份。
本支只回答「**真的畫出來之後，盒與圖貼不貼齊、點得到嗎、右鍵是不是圖**」。

Exit: 0 全過；3 有發現；1 檢查壞了。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# 預設讀樹上那一支。LIGHTBOX_SOURCE 是**驗這支檢查器自己**用的：
# 要證明「已知會紅的狀態它真的會紅」，可以把突變過的複本指給它，
# 這樣綠→紅→綠三段不必去動共用樹上的 markdown.jsx（別的席位可能正在跑那棵樹的套件）。
MARKDOWN_JSX = Path(
    os.environ.get("LIGHTBOX_SOURCE")
    or ROOT / "apps" / "anila-shell" / "src" / "markdown.jsx"
)

BROWSERS = ("google-chrome", "chromium", "chromium-browser", "google-chrome-stable")

# (自然寬, 自然高, 標籤) —— 極端長寬比是重點：letterbox 面積在那裡最大，
# 而接近方形的圖「前提成立與否都會過」。
SHAPES = [(2400, 600, "4:1 極寬"), (600, 2400, "1:4 極高"), (1000, 1000, "1:1 對照")]
VIEWPORTS = [(1920, 1080), (760, 900)]
HUGE = (6000, 1500)          # box=auto 那一幀用：比視窗大很多


def die_broken(msg: str) -> None:
    print(f"BROKEN: {msg}")
    sys.exit(1)


def find_browser() -> str:
    for name in BROWSERS:
        path = shutil.which(name)
        if path:
            return path
    die_broken(
        "找不到 Chrome/Chromium（試過：" + "、".join(BROWSERS) + "）。\n"
        "        幾何要真的畫出來才量得到——**沒有瀏覽器時本檢查回 BROKEN，不回 PASS**。\n"
        "        裝一個系統瀏覽器，或設 PATH 指向既有的那顆。"
    )
    raise AssertionError("unreachable")


def _style_after(src: str, anchor: str) -> str:
    """抽 anchor 之後第一個 style={{ ... }} 的物件字面（括號配對，不用 regex 猜結尾）。"""
    i = src.index(anchor)
    j = src.index("style={{", i)
    depth = 0
    k = j + len("style={")
    while k < len(src):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                break
        k += 1
    return src[j + len("style={"): k + 1]


def _to_css(obj_text: str, drop: tuple[str, ...] = ()) -> str:
    out = []
    for raw in re.sub(r"//[^\n]*", "", obj_text).strip().strip("{}").splitlines():
        line = raw.strip().rstrip(",")
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = re.sub(r"[A-Z]", lambda m: "-" + m.group(0).lower(), key.strip())
        if key in drop:
            continue
        val = val.strip().strip("\"'")
        if re.fullmatch(r"\d+", val) and key in {
            "inset", "padding", "top", "right", "width", "height",
            "border-radius", "font-size",
        }:
            val += "px"
        out.append(f"{key}: {val}")
    return "; ".join(out)


def extract() -> tuple[str, str, str]:
    if not MARKDOWN_JSX.exists():
        die_broken(f"讀不到 {MARKDOWN_JSX}")
    src = MARKDOWN_JSX.read_text(encoding="utf-8")
    m = re.search(r"export function fitLightboxBox[\s\S]*?\n\}", src)
    if not m:
        die_broken("在 markdown.jsx 裡找不到 export function fitLightboxBox —— "
                   "被改名或改成非匯出了，抽取器要跟著改，不要手抄一份。")
    fn = m.group(0).replace("export ", "", 1)
    try:
        backdrop = _to_css(_style_after(src, 'role="dialog"'))
        img = _to_css(_style_after(src, "<img\n        ref={imgRef}"),
                      drop=("width", "height"))
    except ValueError:
        die_broken("抽不到 backdrop／img 的 style 物件 —— 錨點漂了，抽取器要跟著改。")
    return fn, backdrop, img


PAGE = """<!doctype html><meta charset="utf-8"><body style="margin:0;background:#111">
<div id="bd" role="dialog" style="__BACKDROP__"><img id="pic" alt="" style="__IMG__"></div>
<pre id="out">PENDING</pre>
<script>
__FIT__
const img = document.getElementById('pic'), bd = document.getElementById('bd');
let bdClicks = 0;
bd.addEventListener('click', () => bdClicks++);
img.addEventListener('click', (e) => e.stopPropagation());
function mk(w, h) {
  const c = document.createElement('canvas'); c.width = w; c.height = h;
  const g = c.getContext('2d'); g.fillStyle = '#4af'; g.fillRect(0, 0, w, h);
  return c.toDataURL('image/png');
}
function load(w, h) {
  return new Promise((res) => {
    img.onload = () => setTimeout(res, 16);
    img.style.width = 'auto'; img.style.height = 'auto'; img.src = mk(w, h);
  });
}
function applyBox() {
  const b = fitLightboxBox(img.naturalWidth, img.naturalHeight, innerWidth, innerHeight);
  if (b) { img.style.width = b.width + 'px'; img.style.height = b.height + 'px'; }
}
function measure(label) {
  const r = img.getBoundingClientRect();
  const ar = img.naturalWidth / img.naturalHeight;
  const boxAr = r.height ? r.width / r.height : 0;
  const outX = Math.min(r.right + 2, innerWidth - 1), midY = r.top + r.height / 2;
  const outside = document.elementFromPoint(outX, midY);
  const inside = document.elementFromPoint(r.left + 2, midY);
  bdClicks = 0;
  if (outside) outside.dispatchEvent(new MouseEvent('click', {bubbles: true}));
  const closes = bdClicks > 0;
  let ctx = null;
  const h = (e) => { ctx = e.target; };
  document.addEventListener('contextmenu', h, true);
  const hit = document.elementFromPoint(r.left + r.width / 2, midY);
  if (hit) hit.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true}));
  document.removeEventListener('contextmenu', h, true);
  return {
    label, vw: innerWidth, vh: innerHeight,
    nat: [img.naturalWidth, img.naturalHeight], natAr: +ar.toFixed(4),
    box: [Math.round(r.width), Math.round(r.height)], boxAr: +boxAr.toFixed(4),
    skewPct: +(Math.abs(boxAr - ar) / ar * 100).toFixed(2),
    overflows: Math.round(r.width) > innerWidth || Math.round(r.height) > innerHeight,
    outsideHit: outside ? outside.id : null, outsideCloses: closes,
    insideHit: inside ? inside.id : null,
    ctxTarget: ctx ? ctx.tagName : null,
  };
}
(async () => {
  const rows = [];
  for (const [w, h, name] of __SHAPES__) {
    await load(w, h); applyBox();
    await new Promise((r) => setTimeout(r, 16));
    rows.push(measure(name));
  }
  const [hw, hh] = __HUGE__;
  await load(hw, hh);                       // 刻意不 applyBox：重現 onLoad 前那一幀
  rows.push(measure('box=auto 那一幀'));
  document.getElementById('out').textContent = JSON.stringify(rows);
})().catch((e) => {
  document.getElementById('out').textContent = 'ERROR ' + (e && e.message || e);
});
</script>"""


def run_browser(browser: str, page: Path, vw: int, vh: int) -> list[dict]:
    proc = subprocess.run(
        [browser, "--headless", "--disable-gpu", "--no-sandbox",
         f"--window-size={vw},{vh}", "--virtual-time-budget=8000", "--dump-dom",
         page.as_uri()],
        capture_output=True, text=True, timeout=120,
    )
    m = re.search(r'<pre id="out">([\s\S]*?)</pre>', proc.stdout)
    if not m or m.group(1).strip() == "PENDING":
        die_broken(
            f"瀏覽器沒有回傳量測結果（視窗 {vw}x{vh}）。stderr 尾段：\n"
            + (proc.stderr[-400:] or "(空)")
        )
    return json.loads(m.group(1))


def main() -> int:
    browser = find_browser()
    fn, backdrop, img_css = extract()
    print(f"瀏覽器      : {browser}")
    try:
        shown = MARKDOWN_JSX.relative_to(ROOT)
    except ValueError:
        shown = f"{MARKDOWN_JSX}  ⚠ LIGHTBOX_SOURCE 覆寫中（非樹上那一支）"
    print(f"抽自         : {shown}")
    print(f"  backdrop  : {backdrop}")
    print(f"  img       : {img_css}   （width/height 由 fitLightboxBox 決定）")

    html = (PAGE.replace("__BACKDROP__", backdrop).replace("__IMG__", img_css)
                .replace("__FIT__", fn)
                .replace("__SHAPES__", json.dumps(SHAPES))
                .replace("__HUGE__", json.dumps(list(HUGE))))

    findings: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        page = Path(td) / "geometry.html"
        page.write_text(html, encoding="utf-8")
        for vw, vh in VIEWPORTS:
            for row in run_browser(browser, page, vw, vh):
                tag = f"[{vw}x{vh}] {row['label']}"
                print(
                    f"  {tag:26s} 盒 {row['box'][0]}x{row['box'][1]}"
                    f"  比例 {row['boxAr']}（原 {row['natAr']}，歪 {row['skewPct']}%）"
                    f"  圖外命中 {row['outsideHit']}／關 {row['outsideCloses']}"
                    f"  圖內 {row['insideHit']}  右鍵 {row['ctxTarget']}"
                )
                # ① 盒 == 繪製區：圖外一像素必須是暗底且點得關，圖內必須是圖本身
                if row["outsideHit"] != "bd" or not row["outsideCloses"]:
                    findings.append(
                        f"{tag}：圖外 1px 命中 {row['outsideHit']!r}、點擊關閉={row['outsideCloses']}"
                        f" —— 死區回來了（V-2 的形狀）"
                    )
                if row["insideHit"] != "pic":
                    findings.append(f"{tag}：圖內 1px 命中 {row['insideHit']!r}，不是圖本身")
                # ② 長寬比 == 原圖（1% 容差吃捨入；實測正常值 ≤0.4%）
                if row["skewPct"] > 1.0:
                    findings.append(
                        f"{tag}：長寬比歪了 {row['skewPct']}%"
                        f"（盒 {row['boxAr']} vs 原圖 {row['natAr']}）—— R2-2 的形狀"
                    )
                # ③ 任何時刻都不超出視窗，含 box=auto 那一幀
                if row["overflows"]:
                    findings.append(
                        f"{tag}：元素盒 {row['box'][0]}x{row['box'][1]} 超出視窗"
                        f" {row['vw']}x{row['vh']} —— A6-1 的形狀"
                    )
                # ④ 右鍵仍打得到圖（另存圖片／複製圖片）
                if row["ctxTarget"] != "IMG":
                    findings.append(
                        f"{tag}：右鍵 contextmenu 落在 {row['ctxTarget']!r} 而不是 IMG"
                        f" —— 圖上另存被封掉（R-1 的形狀）"
                    )

    print()
    if findings:
        print(f"FINDINGS（{len(findings)}）：")
        for f in findings:
            print(f"  - {f}")
        return 3
    print("PASS：盒貼齊／長寬比／不溢出／右鍵可及，四條在所有情境都成立。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
