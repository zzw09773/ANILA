// ANILA runtime client entry — React + BrowserRouter + AuthProvider + RequireAuth (ESM)
import React from "react";
import ReactDOM from "react-dom/client";
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";

import App from "./app.jsx";
import AdminAccessPage from "./admin/access.jsx";
import { LoginView } from "./login.jsx";
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
  const location = useLocation();

  if (!authReady) {
    return <BootScreen label="正在恢復工作階段…" />;
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }

  return children;
}

function RedirectIfAuthed({ children }) {
  const { authReady, isAuthenticated } = useAuth();

  if (!authReady) {
    return <BootScreen label="正在恢復工作階段…" />;
  }

  if (isAuthenticated) {
    return <Navigate to="/app" replace />;
  }

  return children;
}

function RootRoutes() {
  return (
    <Routes>
      <Route
        path="/login"
        element={
          <RedirectIfAuthed>
            <LoginView />
          </RedirectIfAuthed>
        }
      />
      <Route
        path="/app/admin/access"
        element={
          <RequireAuth>
            <AdminAccessPage />
          </RequireAuth>
        }
      />
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
