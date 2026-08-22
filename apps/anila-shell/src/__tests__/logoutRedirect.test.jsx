import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { loginHref } from "../appOrigins.js";
import { AuthProvider, useLogoutRedirect } from "../runtime/auth.jsx";
import { RequireAuth } from "../runtime/requireAuth.jsx";

const USER = { id: 1, username: "alice", role: "user" };

function jsonOk(body) {
  return {
    ok: true,
    status: 200,
    headers: new Headers({ "content-type": "application/json" }),
    json: async () => body,
  };
}

function LogoutHarness() {
  const onLogout = useLogoutRedirect();
  return (
    <RequireAuth>
      <div>
        <span>工作臺</span>
        <button type="button" onClick={() => void onLogout()}>
          登出
        </button>
      </div>
    </RequireAuth>
  );
}

describe("shell logout", () => {
  let replace;
  let resolveLogout;
  const originalLocation = window.location;

  beforeEach(() => {
    resolveLogout = null;
    replace = vi.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        ancestorOrigins: originalLocation.ancestorOrigins,
        hash: originalLocation.hash,
        host: originalLocation.host,
        hostname: originalLocation.hostname,
        origin: originalLocation.origin,
        pathname: "/app",
        port: originalLocation.port,
        protocol: originalLocation.protocol,
        search: "",
        href: "http://localhost:5175/app",
        replace,
        assign: vi.fn(),
        reload: vi.fn(),
      },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url) => {
        const path = String(url);
        if (path.includes("/api/auth/me")) return jsonOk(USER);
        if (path.includes("/api/auth/logout")) {
          await new Promise((resolve) => {
            resolveLogout = resolve;
          });
          return jsonOk({ ok: true });
        }
        return {
          ok: false,
          status: 401,
          statusText: "Unauthorized",
          headers: new Headers({ "content-type": "text/plain" }),
          text: async () => "HTTP 401",
        };
      }),
    );
  });

  afterEach(() => {
    Object.defineProperty(window, "location", {
      configurable: true,
      value: originalLocation,
    });
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("clears the workbench, waits for logout, then hard-replaces /login without next", async () => {
    render(
      <AuthProvider>
        <LogoutHarness />
      </AuthProvider>,
    );

    expect(await screen.findByText("工作臺")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "登出" }));

    expect(await screen.findByText("正在登出…")).toBeTruthy();
    expect(screen.queryByText("工作臺")).toBeNull();
    expect(replace).not.toHaveBeenCalled();

    resolveLogout();
    await waitFor(() => {
      expect(replace).toHaveBeenCalledTimes(1);
    });
    expect(replace).toHaveBeenCalledWith(`${loginHref()}?logout=1`);
    expect(replace.mock.calls[0][0]).not.toMatch(/next=/);
  });
});
