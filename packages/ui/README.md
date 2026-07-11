# @anila/ui

ANILA React 前端的共用元件層。此套件包含 React 18 primitives；CSP 的 Vue
前端不應直接引用它。跨 React／Vue／純 HTML 的視覺契約位於
`packages/tokens`，由 `@anila/tokens/tokens.css` 提供。

## 內容

- React 元件：`Button`、`IconButton`、`Input`、`Select`、`Modal`、
  `Dropdown`、`Tabs`、`Table`、`Toast`、`AppShell`、`NavGroup`、`Kbd` 等。
- Inline SVG icons：無外部 icon 套件或 CDN。
- `src/styles/tokens.css`：既有 consumer 的相容入口；新程式應直接引用
  `@anila/tokens/tokens.css`。

## 接入方式

repo 尚未採用 JavaScript workspace，因此 app 透過 Vite source alias 引用：

```js
const tokenSrc = fileURLToPath(new URL("../../packages/tokens/src", import.meta.url));
const uiSrc = fileURLToPath(new URL("../../packages/ui/src", import.meta.url));

resolve: {
  alias: { "@anila/tokens": tokenSrc, "@anila/ui": uiSrc },
  dedupe: ["react", "react-dom"],
}
```

```js
import "@anila/tokens/tokens.css";
import { Button, Modal } from "@anila/ui";
```

Docker build context 必須涵蓋 `packages/tokens`、`packages/ui` 與 app 本身。

## 平台邊界

- anila-shell：React，已接入 tokens 與首批 primitives。
- anilalm：React，可逐步接入相同元件與 tokens。
- csp-governance-ui：Vue，只共享 `@anila/tokens`；若要共用元件，應另建 Vue
  adapter/component layer，不能直接引用本套件。

各 app 可先用橋接層把舊 CSS 變數指向 `--anila-*`，再逐頁移除舊名。

## 測試

```bash
cd packages/ui
npm ci
npm test
```

## 暫不處理

跨 repo 發佈、Vue 元件版、密等浮水印等安全元件遷移。這些項目需要獨立契約與
安全審查，不由基礎視覺元件套件隱式承擔。
