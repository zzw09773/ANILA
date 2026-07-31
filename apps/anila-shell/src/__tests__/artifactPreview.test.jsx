// 右側靜態產物預覽：偵測（含 xml 標成 SVG）、sandbox 隔離、密等繼承、預覽／原始碼切換。
import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import React from "react";

import {
  ARTIFACT_IFRAME_SANDBOX,
  buildArtifactSrcDoc,
  detectArtifactKind,
} from "../runtime/artifactDetect.js";
import { ArtifactPanel } from "../artifact.jsx";
import { ArtifactPreviewProvider } from "../artifactContext.jsx";
import { MarkdownView } from "../markdown.jsx";
import { classifiedCopyDenial } from "../uxCopy.js";

afterEach(cleanup);

// 擁有者實例：甜甜圈 SVG，fence 標成 xml 而非 svg。
const DONUT_SVG = `<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="120" height="120">
  <circle cx="50" cy="50" r="40" fill="#d4a574" stroke="#8b5a2b" stroke-width="2"/>
  <circle cx="50" cy="50" r="16" fill="#fff"/>
</svg>`;

const HOSTILE_HTML = `<!DOCTYPE html><html><body>
<script>
try { window.parent.document.cookie; } catch (e) {}
try { window.parent.document.body.innerHTML = "pwned"; } catch (e) {}
fetch("/", { credentials: "include" });
</script>
<p>hostile</p>
</body></html>`;

describe("detectArtifactKind — SVG fenced as xml（擁有者實例）", () => {
  it("detects SVG content labelled xml and marks it previewable as svg", () => {
    expect(detectArtifactKind("xml", DONUT_SVG)).toBe("svg");
  });

  it("detects bare <svg> even with a lying fence", () => {
    expect(detectArtifactKind("text", "<svg xmlns='http://www.w3.org/2000/svg'><circle/></svg>")).toBe("svg");
    expect(detectArtifactKind("python", DONUT_SVG)).toBe("svg");
  });

  it("honours explicit html / markdown tags and sniffs HTML documents", () => {
    expect(detectArtifactKind("html", "<div>hi</div>")).toBe("html");
    expect(detectArtifactKind("md", "# 標題")).toBe("markdown");
    expect(detectArtifactKind("", "<!DOCTYPE html><html><body>x</body></html>")).toBe("html");
  });

  it("does not offer preview for unrelated code", () => {
    expect(detectArtifactKind("python", "print('hi')")).toBeNull();
    expect(detectArtifactKind("xml", "<note><to>A</to></note>")).toBeNull();
  });
});

describe("ArtifactPanel sandbox — 惡意文件碰不到父頁", () => {
  it("renders iframe sandbox without allow-same-origin or allow-scripts", () => {
    const { container } = render(
      <ArtifactPanel
        artifact={{ kind: "html", source: HOSTILE_HTML }}
        classified={false}
        onClose={() => {}}
      />,
    );
    const iframe = container.querySelector('[data-testid="artifact-iframe"]');
    expect(iframe).toBeTruthy();
    // 斷言實際渲染出的屬性，不是註解裡的意圖。
    const sandbox = iframe.getAttribute("sandbox");
    expect(sandbox).toBe(ARTIFACT_IFRAME_SANDBOX);
    expect(sandbox).toBe("");
    expect(sandbox).not.toMatch(/allow-same-origin/);
    expect(sandbox).not.toMatch(/allow-scripts/);
    // srcdoc 有裝進惡意內容——隔離靠 sandbox，不是靠刪 script。
    expect(iframe.getAttribute("srcdoc")).toContain("<script>");
    expect(iframe.getAttribute("srcdoc")).toContain("window.parent.document.cookie");
  });

  it("buildArtifactSrcDoc keeps SVG inside an HTML shell for srcdoc", () => {
    const doc = buildArtifactSrcDoc("svg", DONUT_SVG);
    expect(doc).toContain("<svg");
    expect(doc).toContain("<!DOCTYPE html>");
  });
});

describe("ArtifactPanel classification — 與訊息同一套門檻", () => {
  it("watermarks 密／機密 and denies copy with the same denial copy", () => {
    for (const level of ["密", "機密"]) {
      const { container } = render(
        <ArtifactPanel
          artifact={{ kind: "svg", source: DONUT_SVG }}
          classified={true}
          classificationLevel={level}
          onClose={() => {}}
        />,
      );
      const panel = container.querySelector('[data-testid="artifact-panel"]');
      expect(panel.getAttribute("data-classified")).toBe("true");
      expect(panel.getAttribute("data-classification-level")).toBe(level);
      // ClassificationWatermark 寫 data-classification={level}
      expect(container.querySelector(`[data-classification="${level}"]`)).toBeTruthy();
      const denied = screen.getByTestId("artifact-copy-denied");
      expect(denied).toBeDisabled();
      expect(denied.getAttribute("title")).toBe(classifiedCopyDenial(level));
      expect(screen.queryByTestId("artifact-copy")).toBeNull();
      cleanup();
    }
  });

  it("allows copy when the conversation is not classified", () => {
    render(
      <ArtifactPanel
        artifact={{ kind: "markdown", source: "# hi" }}
        classified={false}
        classificationLevel="無機密"
        onClose={() => {}}
      />,
    );
    expect(screen.getByTestId("artifact-copy")).toBeTruthy();
    expect(screen.queryByTestId("artifact-copy-denied")).toBeNull();
    expect(screen.queryByTestId("artifact-panel").querySelector("[data-classification]")).toBeNull();
  });
});

describe("ArtifactPanel source ↔ preview toggle", () => {
  it("lets the user move between rendered form and source and back", () => {
    render(
      <ArtifactPanel
        artifact={{ kind: "svg", source: DONUT_SVG }}
        classified={false}
        onClose={() => {}}
      />,
    );
    expect(screen.getByTestId("artifact-iframe")).toBeTruthy();
    expect(screen.queryByTestId("artifact-source")).toBeNull();

    fireEvent.click(screen.getByTestId("artifact-tab-source"));
    expect(screen.getByTestId("artifact-source").textContent).toContain("<svg");
    expect(screen.queryByTestId("artifact-iframe")).toBeNull();

    fireEvent.click(screen.getByTestId("artifact-tab-preview"));
    expect(screen.getByTestId("artifact-iframe")).toBeTruthy();
    expect(screen.queryByTestId("artifact-source")).toBeNull();
  });

  it("closes from the header control", () => {
    let closed = false;
    render(
      <ArtifactPanel
        artifact={{ kind: "html", source: "<!DOCTYPE html><html><body>x</body></html>" }}
        onClose={() => { closed = true; }}
      />,
    );
    fireEvent.click(screen.getByLabelText("關閉預覽"));
    expect(closed).toBe(true);
  });
});

describe("CodeBlock preview affordance — 使用者選擇才開", () => {
  it("shows 預覽 on an xml-fenced SVG and opens via provider", () => {
    const opened = [];
    const md = "```xml\n" + DONUT_SVG + "\n```";
    render(
      <ArtifactPreviewProvider onOpen={(a) => opened.push(a)}>
        <MarkdownView text={md} />
      </ArtifactPreviewProvider>,
    );
    const btn = screen.getByTestId("artifact-preview-btn");
    expect(btn.textContent).toContain("預覽");
    fireEvent.click(btn);
    expect(opened).toHaveLength(1);
    expect(opened[0].kind).toBe("svg");
    expect(opened[0].source).toContain("<svg");
  });

  it("does not show 預覽 for ordinary python", () => {
    render(
      <ArtifactPreviewProvider onOpen={() => {}}>
        <MarkdownView text={"```python\nprint(1)\n```"} />
      </ArtifactPreviewProvider>,
    );
    expect(screen.queryByTestId("artifact-preview-btn")).toBeNull();
  });
});
