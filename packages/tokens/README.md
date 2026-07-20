# @anila/tokens

ANILA 三個前端共用、與框架無關的 CSS design tokens。Vue、React 或純 HTML
都可直接載入 `src/tokens.css`，不依賴 React，也不包含外網字型或 CDN 資源。

```js
import "@anila/tokens/tokens.css";
```

目前以 repo 內 Vite alias 接入；`@anila/ui/styles/tokens.css` 僅保留作為既有
React consumer 的相容入口。
