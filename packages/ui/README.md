# @anila/ui — 三平台共用設計系統基礎

CSP／ANILA／ANILALM 三平台前端重設計的第一個切片：design tokens ＋ 核心 React 元件。
設計語彙依 [doc 12](../../docs/anila-redesign-docs/12-frontend-visual-redesign.md)：
**淺色優先・官方藍 institutional**（accent `#2b4c7e`、中性冷灰、溫圓角、系統字型堆疊、
等寬字保留給 ID／數字／代碼）。

## 內容

- **Design tokens**（`src/styles/tokens.css`，命名空間 `--anila-*`）：
  colors／typography／spacing／radius／shadow／z-index 六類。
  值掛在 `:root, [data-theme="light"]`；未來第二主題（深色）只需追加
  `[data-theme="dark"]` 覆蓋段，元件不用改。本切片僅交付淺色一套主題。
- **核心元件**（`src/components/`，React 18／JSX／inline style）：
  `Button`、`IconButton`、`Input`、`Select`、`Modal`、`Dropdown`＋`MenuItem`、
  `Tabs`、`Table`、`Toast`＋`ToastStack`、`AppShell`＋`Sidebar`＋`Topbar`、
  `NavGroup`＋`NavItem`、`Kbd`、`Divider`。每個元件在 `src/__tests__/` 有
  render test。
- **零外網資源**：無 webfont、無 CDN、icons 全 inline SVG——與三平台既有
  air-gap 基線一致。

## 接入方式（源碼 alias，非 npm 安裝）

repo 沒有 JS workspace；本套件以「原始碼」形式被各 app 的 Vite alias 引用，
由 app 自己的 Vite pipeline 編譯（參考 `apps/anila-shell/vite.config.js`）：

```js
// vite.config.js
resolve: {
  alias: { "@anila/ui": path.resolve(__dirname, "../../packages/ui/src") },
  // react 一律解析到 app 自己的 node_modules，避免雙 React instance
  dedupe: ["react", "react-dom"],
},
```

```js
// app 進入點載入 tokens（一次）
import "@anila/ui/styles/tokens.css";
// 元件
import { Button, Modal } from "@anila/ui";
```

Docker build 注意：app 的 build context 必須涵蓋 `packages/ui`
（anila-shell 已改用 repo-root context，見 `infra/compose/platform.yml`）。

## 各 app 橋接慣例

各 app 用自己的橋接層把既有變數指到 `--anila-*`，業務碼不用改：

- **anila-shell**（pilot，已接）：`index.html` 的 `:root` 把 `--bg`／`--fg`／
  `--accent` 等舊名映射為 `var(--anila-*)`。
- **csp-governance-ui**（未遷移）：後續把 `--c-*`（tokens.css）值收斂到本套件。
- **anilalm**（未遷移）：後續把 `src/theme/tokens.ts` 的 JS token 收斂到本套件。

## 測試

```bash
cd packages/ui && npm install && npm test
```

## 邊界（本切片刻意不做）

深色主題實作、Vue 元件版（治理中心）、跨 repo 發佈機制、
密等浮水印元件收編（`trust.jsx` 是安全元件，遷移需另案審查）。
