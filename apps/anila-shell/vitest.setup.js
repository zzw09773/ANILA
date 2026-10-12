import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// Sprint 13 PR B2: ensure RTL unmounts components between tests so the
// jsdom body doesn't accumulate siblings (which trips getByRole's
// "found multiple elements" guard).
afterEach(() => {
  cleanup();
});

// jsdom 沒有實作 `Element.prototype.scrollTo`(只有 window 上有)。
// app.jsx 的 autoscroll effect 會對訊息容器呼叫它,在 jsdom 下直接
// TypeError,整個 orchestrator 掛不起來——這是環境缺口,不是產品缺陷。
// 補成 no-op;真實瀏覽器行為不受影響(這個檔案不進 build)。
if (typeof Element !== "undefined" && !Element.prototype.scrollTo) {
  Element.prototype.scrollTo = function scrollTo() {};
}

// jsdom 沒有 URL.revokeObjectURL。下載流程用 setTimeout(..., 1000) 延後撤銷;
// 測試在 finally 把它還原成 undefined 後,機器忙時計時器才觸發,就變成
// 「2 unhandled errors」讓整個 npm test 失敗(平行跑全套時重現過)。給一個 no-op 底。
if (typeof URL.revokeObjectURL !== "function") URL.revokeObjectURL = () => {};
