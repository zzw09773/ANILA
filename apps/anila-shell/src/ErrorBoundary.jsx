// 根部錯誤網子（finding-no-error-boundary-20260820，HIGH）。
//
// render 期例外以前會讓 React 卸載整棵樹：白畫面、零訊息，而且「白畫面不會
// 報錯」——事故看起來跟「本來就沒有這個面板」一模一樣。這裡照 anilalm 的
// ErrorBoundary 形態，但畫面內容照內網鐵則收窄：**一句可讀訊息＋時間戳**，
// 不印 error.message、stack、路徑或主機名（那些只進 console，給開發者看）。
// 沒有關聯碼：本 app 沒有把前端錯誤送到伺服器的通道，假的追蹤碼比沒有更糟。
//
// 突變登記：scripts/mutation-check.mjs `error-boundary-fallback-dropped`
// （把 fallback 渲染換回 children → errorBoundary.test.jsx 必紅）。

import React from "react";

export function describeErrorForUser(at = new Date()) {
  return `系統發生錯誤，請重新整理或聯繫維運。時間：${at.toISOString()}`;
}

export class ErrorBoundary extends React.Component {
  state = { failedAt: null };

  static getDerivedStateFromError() {
    return { failedAt: new Date() };
  }

  componentDidCatch(error, info) {
    // 開發者通道：完整錯誤與元件堆疊只到 console，不上畫面。
    console.error("[ANILA crash]", error);
    console.error("[ANILA component stack]", info?.componentStack);
  }

  reload = () => {
    window.location.reload();
  };

  render() {
    if (this.state.failedAt) {
      return (
        <div role="alert" className="anila-error-boundary">
          <p>{describeErrorForUser(this.state.failedAt)}</p>
          <button type="button" onClick={this.reload}>
            重新整理
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
