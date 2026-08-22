// ANILA runtime client entry — React + BrowserRouter + AuthProvider + RequireAuth (ESM)
//
// branch SSO：本 SPA 不再持有登入頁，唯一登入入口是 myCSPPlatform Vue SPA
// (路徑 /login)。RequireAuth 在 unauthenticated 時走 ``window.location.assign``
// 跳出 SPA，讓 nginx 把 /login 路由到 csp_backend serve 出 LoginView.vue。
// React Router 的 ``<Navigate to="/login">`` 不適用 — /login 不在本 SPA
// 路由表內，client-side navigate 會被 catch-all 重導回 /app 死循環。
import React from "react";
import ReactDOM from "react-dom/client";
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
} from "react-router";

import "../../shared/tokens.css";
import "../../shared/chrome.css";
import App from "./app.jsx";
import { AuthProvider } from "./runtime/auth.jsx";
import { RequireAuth } from "./runtime/requireAuth.jsx";
import { ConfirmProvider } from "./confirm.jsx";

function RootRoutes() {
  return (
    <Routes>
      <Route
        path="/app/*"
        element={
          <RequireAuth>
            <App />
          </RequireAuth>
        }
      />
      <Route path="*" element={<Navigate to="/app" replace />} />
    </Routes>
  );
}

const container = document.getElementById("root");
if (!container) {
  throw new Error("#root element not found in index.html");
}

// BASE_URL 來自 Vite 的 base 設定:本機 dev 是 '/',正式部署(ANILA 反向
// proxy 同源 subpath)是 '/anila/'。React Router 的 basename 不要尾斜線。
// 同 ANILALM App.tsx 的慣例。
const ROUTER_BASENAME = import.meta.env.BASE_URL.replace(/\/$/, "") || undefined;

ReactDOM.createRoot(container).render(
  <React.StrictMode>
    <BrowserRouter basename={ROUTER_BASENAME}>
      <AuthProvider>
        <ConfirmProvider>
          <RootRoutes />
        </ConfirmProvider>
      </AuthProvider>
    </BrowserRouter>
  </React.StrictMode>,
);
