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
} from "react-router-dom";

import App from "./app.jsx";
import { AuthProvider, useAuth } from "./runtime/auth.jsx";

function BootScreen({ label = "啟動中…" }) {
  return (
    <div className="boot-screen" role="status" aria-live="polite">
      <span style={{ opacity: 0.7 }}>{label}</span>
      <span aria-hidden style={{ marginLeft: 6, animation: "anila-blink 1s step-end infinite" }}>
        _
      </span>
    </div>
  );
}

function RequireAuth({ children }) {
  const { authReady, isAuthenticated } = useAuth();

  if (!authReady) {
    return <BootScreen label="正在恢復工作階段…" />;
  }

  if (!isAuthenticated) {
    // Full-page navigation 跳到 CSP 平台 login，並夾帶 next 讓登入後跳回。
    // 用 useEffect 而非渲染期直接執行，避免 React commit phase 副作用警告。
    return <RedirectToCspLogin />;
  }

  return children;
}

function RedirectToCspLogin() {
  React.useEffect(() => {
    // 用 absolute URL with current port — anila-ui 通常跑在 4443，但 LoginView
    // 在 443 (myCSPPlatform Vue SPA + assets 都在那)。next 帶完整 URL 包含
    // 4443 port，登入完 LoginView 才能跨 port 把使用者送回原本的 anila-ui。
    const currentHref = window.location.href;
    const loginOrigin = `${window.location.protocol}//${window.location.hostname}`; // 443/80 default
    const target = `${loginOrigin}/login?next=${encodeURIComponent(currentHref)}`;
    window.location.assign(target);
  }, []);
  return <BootScreen label="導向 CSP 平台登入…" />;
}

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

ReactDOM.createRoot(container).render(
  <React.StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <RootRoutes />
      </AuthProvider>
    </BrowserRouter>
  </React.StrictMode>,
);
