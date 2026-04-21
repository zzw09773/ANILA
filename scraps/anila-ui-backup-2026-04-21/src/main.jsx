import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import "./styles.css";
import { AuthProvider, useAuth } from "./runtime/auth.jsx";
import { RuntimePage } from "./runtime/app-page.jsx";
import { LoginPage } from "./runtime/login-page.jsx";

function RequireAuth({ children }) {
  const { isAuthenticated, authReady } = useAuth();
  if (!authReady) {
    return <div className="boot-screen">載入中…</div>;
  }
  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }
  return children;
}

function AppRouter() {
  const { isAuthenticated } = useAuth();

  return (
    <Routes>
      <Route
        path="/"
        element={<Navigate to={isAuthenticated ? "/app" : "/login"} replace />}
      />
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/app"
        element={
          <RequireAuth>
            <RuntimePage />
          </RequireAuth>
        }
      />
    </Routes>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <AppRouter />
      </AuthProvider>
    </BrowserRouter>
  </React.StrictMode>,
);
