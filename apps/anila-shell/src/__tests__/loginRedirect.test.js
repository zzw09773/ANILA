import { describe, expect, it } from "vitest";

import { cspLoginHref } from "../runtime/loginRedirect.js";

describe("cspLoginHref", () => {
  it("未登入導向留在 8443，不會被送去 443", () => {
    const href = cspLoginHref({
      pathname: "/anila/",
      search: "",
      hash: "",
    });
    const landed = new URL(href, "https://lab.example:8443/anila/");
    expect(href.startsWith("/")).toBe(true);
    expect(landed.origin).toBe("https://lab.example:8443");
    expect(landed.port).toBe("8443");
    expect(landed.pathname).toBe("/login");
    expect(landed.searchParams.get("next")).toBe("/anila/");
  });

  it("預設 443 也留在原本的來源", () => {
    const href = cspLoginHref({
      pathname: "/anila/app",
      search: "?q=1",
      hash: "#top",
    });
    const landed = new URL(href, "https://lab.example/anila/app?q=1#top");
    expect(landed.port).toBe("");
    expect(landed.pathname).toBe("/login");
    expect(landed.searchParams.get("next")).toBe("/anila/app?q=1#top");
  });

  it("登出不重組絕對網址，8444 留得住", () => {
    const href = cspLoginHref(null, { carryNext: false });
    const landed = new URL(href, "https://lab.example:8444/anila/app");
    expect(landed.origin).toBe("https://lab.example:8444");
    expect(landed.pathname).toBe("/login");
    expect(landed.search).toBe("");
  });
});

