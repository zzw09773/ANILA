import React from "react";

import { loginHref } from "../appOrigins.js";
import { useAuth } from "./auth.jsx";

export function BootScreen({ label = "載入中…" }) {
  return (
    <div className="boot-screen" role="status" aria-live="polite">
      <span style={{ opacity: 0.75 }}>{label}</span>
    </div>
  );
}

// Cold visit to /app without a session: leave this SPA and keep next= so
// login can send the user back. Logout must NOT come through here — that
// path would set next=/app while cookies are still valid, and the login
// page would bounce a regular user straight back onto the workbench.
function RedirectToCspLogin() {
  React.useEffect(() => {
    window.location.replace(loginHref(window.location.href));
  }, []);
  return <BootScreen label="前往登入頁…" />;
}

export function RequireAuth({ children }) {
  const { authReady, isAuthenticated, loggingOut } = useAuth();

  if (loggingOut) {
    return <BootScreen label="正在登出…" />;
  }

  if (!authReady) {
    return <BootScreen label="正在恢復工作階段…" />;
  }

  if (!isAuthenticated) {
    return <RedirectToCspLogin />;
  }

  return children;
}
