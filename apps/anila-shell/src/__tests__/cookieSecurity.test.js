import { afterEach, describe, expect, it } from "vitest";

import { csrfCookieName, readCsrfCookie } from "../runtime/api.js";


function expire(name) {
  document.cookie = `${name}=; Max-Age=0; Path=/`;
}


afterEach(() => {
  expire("anila_dev_csrf");
  expire("anila_csrf");
});


describe("host-only cookie naming", () => {
  it("selects __Host- for HTTPS and non-loopback HTTP", () => {
    expect(csrfCookieName({ protocol: "https:", hostname: "anila.ai.ncsist.org.tw" })).toBe(
      "__Host-anila_csrf",
    );
    expect(csrfCookieName({ protocol: "http:", hostname: "10.53.100.15" })).toBe(
      "__Host-anila_csrf",
    );
  });

  it("selects the explicit dev namespace only for loopback HTTP", () => {
    expect(csrfCookieName({ protocol: "http:", hostname: "localhost" })).toBe(
      "anila_dev_csrf",
    );
  });

  it("never falls back to the legacy unprefixed cookie", () => {
    document.cookie = "anila_csrf=attacker-controlled; Path=/";
    expect(readCsrfCookie()).toBe("");

    document.cookie = "anila_dev_csrf=synthetic-dev-token; Path=/";
    expect(readCsrfCookie()).toBe("synthetic-dev-token");
  });
});
