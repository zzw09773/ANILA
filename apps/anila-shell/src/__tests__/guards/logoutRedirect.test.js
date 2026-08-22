// @source-text-guard — 本檔含「讀原始碼 + toContain」的字串比對測試。
// 這類斷言只證明某段文字還在檔案裡,**不證明它在執行時會發生**。
// 行為覆蓋在 logoutRedirect.test.jsx。

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

describe("shell logout source guard", () => {
  it("clears identity before the network and hard-replaces /login without next", () => {
    const auth = readFileSync("src/runtime/auth.jsx", "utf8");
    const gate = readFileSync("src/runtime/requireAuth.jsx", "utf8");
    const main = readFileSync("src/main.jsx", "utf8");
    const logoutAt = auth.indexOf("const logout = useCallback");
    expect(auth.indexOf("setLoggingOut(true)", logoutAt)).toBeGreaterThan(logoutAt);
    expect(auth.indexOf("setUser(null)", logoutAt)).toBeGreaterThan(
      auth.indexOf("setLoggingOut(true)", logoutAt),
    );
    expect(auth.indexOf('authRequest("/api/auth/logout"', logoutAt)).toBeGreaterThan(
      auth.indexOf("setUser(null)", logoutAt),
    );
    expect(auth).toMatch(/location\.replace\(`\$\{loginHref\(\)\}\?logout=1`\)/);
    expect(auth).not.toMatch(/loginHref\(window\.location/);
    expect(gate).toMatch(/loggingOut/);
    expect(gate).toMatch(/正在登出/);
    expect(main).toMatch(/RequireAuth/);
    expect(main).not.toMatch(/loginHref\(window\.location/);
  });
});
