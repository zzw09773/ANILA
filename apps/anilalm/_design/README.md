# `_design/` — 設計檔案歸檔（不參與 build）

這個目錄是這次 ANILALM 從 prototype → production 過程中保留的設計來源。Vite 不會
import 這裡的任何檔案，所以放著無害；保留是為了未來 redesign 時有個比對基線。

## 內容

| 檔案 | 用途 |
| --- | --- |
| `prototype.html` | 1929 行的 single-file React 原型，所有資料寫死，CDN 載 React + Babel-standalone |
| `app.jsx` | Theme tokens + Icon set + ThemeSwitch（已 port 到 `src/theme/` + `src/components/Icon.tsx`） |
| `design-canvas.jsx` | Figma-like canvas wrapper（pan/zoom/artboard reorder/focus mode）— 純設計 review 工具 |
| `screens/login.jsx` 等 5 支 | 個別畫面的靜態 artboard，曾被嵌進 `design-canvas` 排版 |

## 想看 prototype 怎麼跑？

```bash
cd /home/aia/c1147259/ANILA/ANILALM/_design
python3 -m http.server 8088
# 開 http://localhost:8088/prototype.html
```

> 提醒：prototype 裡所有「登入」、「對話」、「生成」都是假的 `setTimeout`；要真功能
> 請跑 `npm run dev`（從上一層）。
