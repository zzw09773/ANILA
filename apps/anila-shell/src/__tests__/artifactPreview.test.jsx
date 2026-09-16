// 右側靜態產物預覽：偵測（含 xml 標成 SVG）、sandbox 隔離、密等繼承、預覽／原始碼切換。
import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import React from "react";

import {
  ARTIFACT_IFRAME_SANDBOX,
  ARTIFACT_IFRAME_SANDBOX_HTML,
  artifactFrameSrc,
  buildArtifactFrameProps,
  buildArtifactSrcDoc,
  detectArtifactKind,
  isIncompleteArtifactHtml,
  wrapJsxAsHtml,
} from "../runtime/artifactDetect.js";
import {
  artifactDownloadFilename,
  artifactDownloadSpec,
  downloadArtifactSource,
  prepareArtifactDownload,
} from "../runtime/artifactDownload.js";
import {
  artifactStillNeedsCdn,
  inlineVendorScripts,
  localizeArtifactHtml,
  resolveVendorSrc,
} from "../runtime/artifactVendor.js";
import { ArtifactPanel, clampPanelWidth } from "../artifact.jsx";
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

describe("buildArtifactFrameProps", () => {
  it("puts HTML on the same-origin preview shell, not a data URL", () => {
    const frame = buildArtifactFrameProps("html", HOSTILE_HTML);
    expect(frame.sandbox).toBe(ARTIFACT_IFRAME_SANDBOX_HTML);
    expect(frame.src).toBe(artifactFrameSrc());
    expect(frame.src).toMatch(/artifact-frame\.html$/);
    expect(frame.src).not.toMatch(/^data:/);
    expect(frame.srcDoc).toBeUndefined();
    expect(frame.html).toContain("<script>");
  });

  it("rewrites Three.js CDN and same-folder filenames to the intranet vendor", () => {
    const html = `<!DOCTYPE html><html><head></head><body>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
</body></html>`;
    const out = localizeArtifactHtml(html, { baseUrl: "/anila/" });
    expect(out).toContain("/anila/vendor/three/r128/three.min.js");
    expect(out).toContain("/anila/vendor/three/r128/OrbitControls.js");
    expect(out).not.toMatch(/cdnjs\.cloudflare\.com|cdn\.jsdelivr\.net/);
    expect(artifactStillNeedsCdn(out)).toBe(false);

    const relative = localizeArtifactHtml(
      `<script src="three.min.js"></script><script src="./OrbitControls.js"></script>`,
      { baseUrl: "/anila/" },
    );
    expect(relative).toContain('src="/anila/vendor/three/r128/three.min.js"');
    expect(relative).toContain('src="/anila/vendor/three/r128/OrbitControls.js"');

    const frame = buildArtifactFrameProps("html", html);
    expect(frame.html).toContain("/vendor/three/r128/three.min.js");
    expect(frame.html).not.toContain("cdnjs.cloudflare.com");
  });

  it("rewrites React and Babel CDN and same-folder filenames to the intranet vendor", () => {
    const html = `<!DOCTYPE html><html><head></head><body>
<script src="https://unpkg.com/react@18.3.1/umd/react.production.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/react-dom@18.3.1/umd/react-dom.production.min.js"></script>
<script src="https://unpkg.com/@babel/standalone@7.26.10/babel.min.js"></script>
</body></html>`;
    const out = localizeArtifactHtml(html, { baseUrl: "/anila/" });
    expect(out).toContain("/anila/vendor/react/18.3.1/react.production.min.js");
    expect(out).toContain("/anila/vendor/react/18.3.1/react-dom.production.min.js");
    expect(out).toContain("/anila/vendor/babel/7.26.10/babel.min.js");
    expect(out).not.toMatch(/unpkg\.com|cdn\.jsdelivr\.net/);
    expect(artifactStillNeedsCdn(out)).toBe(false);

    const relative = localizeArtifactHtml(
      `<script src="react.production.min.js"></script><script src="./babel.min.js"></script>`,
      { baseUrl: "/anila/" },
    );
    expect(relative).toContain('src="/anila/vendor/react/18.3.1/react.production.min.js"');
    expect(relative).toContain('src="/anila/vendor/babel/7.26.10/babel.min.js"');
  });

  it("still flags leftover non-Three CDNs after localize", () => {
    const html = `<!DOCTYPE html><html><script src="https://cdn.jsdelivr.net/npm/chart.js"></script></html>`;
    expect(artifactStillNeedsCdn(localizeArtifactHtml(html, { baseUrl: "/anila/" }))).toBe(true);
  });

  it("flags truncated HTML that never closed its script or html tags", () => {
    expect(isIncompleteArtifactHtml("<!DOCTYPE html><html><script>function animate(){")).toBe(true);
    expect(isIncompleteArtifactHtml(HOSTILE_HTML)).toBe(false);
  });

  it("keeps SVG on srcdoc with no scripts", () => {
    const frame = buildArtifactFrameProps("svg", DONUT_SVG);
    expect(frame.sandbox).toBe(ARTIFACT_IFRAME_SANDBOX);
    expect(frame.srcDoc).toContain("<svg");
    expect(frame.src).toBeUndefined();
  });
});

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

  it("detects JSX fences and React components as jsx", () => {
    const app = `function App() {\n  return (\n    <h1>太陽系</h1>\n  );\n}`;
    expect(detectArtifactKind("jsx", app)).toBe("jsx");
    expect(detectArtifactKind("react", app)).toBe("jsx");
    expect(detectArtifactKind("", `import React from "react";\n${app}`)).toBe("jsx");
  });
});

describe("wrapJsxAsHtml — 內網 React／Babel 殼", () => {
  it("wraps a bare App component onto vendor scripts and mounts App", () => {
    const html = wrapJsxAsHtml("function App() { return <h1>hi</h1>; }", { baseUrl: "/anila/" });
    expect(html).toContain("/anila/vendor/react/18.3.1/react.production.min.js");
    expect(html).toContain("/anila/vendor/react/18.3.1/react-dom.production.min.js");
    expect(html).toContain("/anila/vendor/babel/7.26.10/babel.min.js");
    expect(html).toContain('type="text/babel"');
    expect(html).toContain("function App()");
    expect(html).toContain("ReactDOM.createRoot");
    expect(html).not.toMatch(/unpkg\.com|cdn\.jsdelivr\.net/);
  });

  it("includes the typescript preset so typed tsx does not fail to transpile", () => {
    const src = `function App(props: { n: number }) { return <h1>{props.n}</h1>; }`;
    expect(detectArtifactKind("tsx", src)).toBe("jsx");
    const html = wrapJsxAsHtml(src, { baseUrl: "/anila/" });
    expect(html).toContain('data-presets="react,typescript"');
    expect(html).toContain("props: { n: number }");
    expect(html).toContain("/anila/vendor/babel/7.26.10/babel.min.js");
  });

  it("does not add a second mount when the source already calls ReactDOM", () => {
    const src = `function App(){return <p/>}\nReactDOM.createRoot(document.getElementById("root")).render(<App/>);`;
    const html = wrapJsxAsHtml(src, { baseUrl: "/anila/" });
    expect(html.match(/ReactDOM\.createRoot/g) || []).toHaveLength(1);
  });

  it("puts JSX on the same-origin preview shell", () => {
    const frame = buildArtifactFrameProps("jsx", "function App(){return <h1>hi</h1>;}");
    expect(frame.sandbox).toBe(ARTIFACT_IFRAME_SANDBOX_HTML);
    expect(frame.src).toMatch(/artifact-frame\.html$/);
    expect(frame.html).toContain("react.production.min.js");
  });
});

describe("artifact download spec", () => {
  it("maps kinds to the file extensions in the plan", () => {
    expect(artifactDownloadSpec("html")).toEqual({ ext: "html", mime: "text/html" });
    expect(artifactDownloadSpec("svg")).toEqual({ ext: "svg", mime: "image/svg+xml" });
    expect(artifactDownloadSpec("markdown")).toEqual({ ext: "md", mime: "text/markdown" });
    expect(artifactDownloadSpec("jsx")).toEqual({ ext: "html", mime: "text/html" });
    expect(artifactDownloadFilename("svg")).toBe("anila-artifact.svg");
    expect(artifactDownloadFilename("jsx")).toBe("anila-artifact.html");
  });
});

describe("artifact download — file:// 可開", () => {
  it("resolves vendor paths against the page origin", () => {
    expect(resolveVendorSrc("/anila/vendor/three/r128/three.min.js", "https://anila.test")).toBe(
      "https://anila.test/anila/vendor/three/r128/three.min.js",
    );
    expect(resolveVendorSrc("https://anila.test/anila/vendor/three/r128/three.min.js", "https://other")).toBe(
      "https://anila.test/anila/vendor/three/r128/three.min.js",
    );
  });

  it("inlines Three.js vendor scripts so a downloaded file does not need three.min.js beside it", async () => {
    const fetched = [];
    const fetchImpl = async (url) => {
      fetched.push(url);
      const body = url.includes("OrbitControls")
        ? "window.OrbitControls = function () {};"
        : "window.THREE = { Scene: function () {} }; //# sourceMappingURL=three.min.js.map";
      return { ok: true, text: async () => body };
    };
    const html = `<!DOCTYPE html><html><head></head><body>
<script src="three.min.js"></script>
<script src="./OrbitControls.js"></script>
<script>new THREE.Scene();</script>
</body></html>`;
    const { content, filename, mime } = await prepareArtifactDownload(html, "html", {
      baseUrl: "/anila/",
      origin: "https://anila.test",
      fetchImpl,
    });
    expect(filename).toBe("anila-artifact.html");
    expect(mime).toBe("text/html");
    expect(fetched).toContain("https://anila.test/anila/vendor/three/r128/three.min.js");
    expect(fetched).toContain("https://anila.test/anila/vendor/three/r128/OrbitControls.js");
    expect(content).toContain("window.THREE = { Scene: function () {} };");
    expect(content).toContain("window.OrbitControls = function () {};");
    expect(content).not.toMatch(/sourceMappingURL/);
    expect(content).not.toMatch(/\bsrc=["'][^"']*three\.min\.js/);
    expect(content).not.toMatch(/\bsrc=["'][^"']*OrbitControls/);
    expect(content).toContain("new THREE.Scene();");
  });

  it("falls back to an absolute vendor URL when the script cannot be fetched", async () => {
    const html = `<script src="/anila/vendor/three/r128/three.min.js"></script>`;
    const out = await inlineVendorScripts(html, {
      origin: "https://anila.test",
      fetchImpl: async () => {
        throw new Error("offline");
      },
    });
    expect(out).toContain('src="https://anila.test/anila/vendor/three/r128/three.min.js"');
  });

  it("wraps JSX downloads into a self-contained HTML shell", async () => {
    const fetchImpl = async () => ({ ok: true, text: async () => "/* vendor */" });
    const { content, filename } = await prepareArtifactDownload(
      "function App() { return <h1>太陽系</h1>; }",
      "jsx",
      { baseUrl: "/anila/", origin: "https://anila.test", fetchImpl },
    );
    expect(filename).toBe("anila-artifact.html");
    expect(content).toContain("function App()");
    expect(content).toContain("/* vendor */");
    expect(content).toContain('data-presets="react,typescript"');
    expect(content).not.toMatch(/\bsrc=["'][^"']*react\.production/);
  });
});

describe("ArtifactPanel sandbox — 惡意文件碰不到父頁", () => {
  it("runs HTML scripts in a unique origin without allow-same-origin", () => {
    const { container } = render(
      <ArtifactPanel
        artifact={{ kind: "html", source: HOSTILE_HTML }}
        classified={false}
        onClose={() => {}}
      />,
    );
    const iframe = container.querySelector('[data-testid="artifact-iframe"]');
    expect(iframe).toBeTruthy();
    const sandbox = iframe.getAttribute("sandbox");
    expect(sandbox).toBe(ARTIFACT_IFRAME_SANDBOX_HTML);
    expect(sandbox).toMatch(/allow-scripts/);
    expect(sandbox).not.toMatch(/allow-same-origin/);
    const src = iframe.getAttribute("src") || "";
    expect(src).toMatch(/artifact-frame\.html$/);
    expect(src).not.toMatch(/^data:/);
    expect(iframe.getAttribute("srcdoc")).toBeNull();
  });

  it("keeps SVG on a scriptless srcdoc frame", () => {
    const { container } = render(
      <ArtifactPanel
        artifact={{ kind: "svg", source: DONUT_SVG }}
        classified={false}
        onClose={() => {}}
      />,
    );
    const iframe = container.querySelector('[data-testid="artifact-iframe"]');
    expect(iframe.getAttribute("sandbox")).toBe(ARTIFACT_IFRAME_SANDBOX);
    expect(iframe.getAttribute("srcdoc")).toContain("<svg");
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
      const downloadDenied = screen.getByTestId("artifact-download-denied");
      expect(downloadDenied).toBeDisabled();
      expect(downloadDenied.getAttribute("title")).toBe(classifiedCopyDenial(level));
      expect(screen.queryByTestId("artifact-download")).toBeNull();
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
    expect(screen.getByTestId("artifact-download")).toBeTruthy();
    expect(screen.queryByTestId("artifact-copy-denied")).toBeNull();
    expect(screen.queryByTestId("artifact-download-denied")).toBeNull();
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

  it("exposes a drag handle and a width clamp so the panel is not a fixed 420px", () => {
    render(
      <ArtifactPanel
        artifact={{ kind: "html", source: "<!DOCTYPE html><html><body>x</body></html>" }}
        onClose={() => {}}
      />,
    );
    const handle = screen.getByTestId("artifact-resize-handle");
    expect(handle).toBeTruthy();
    expect(handle.getAttribute("role")).toBe("separator");
    expect(screen.getByTestId("artifact-panel").style.width).toBeTruthy();
    const inner = window.innerWidth;
    expect(clampPanelWidth(inner - 400)).toBeGreaterThan(420);
    expect(clampPanelWidth(100)).toBe(280);
  });

  it("warns when HTML still points at a non-Three CDN after localize", () => {
    render(
      <ArtifactPanel
        artifact={{
          kind: "html",
          source: "<!DOCTYPE html><html><script src=\"https://cdn.jsdelivr.net/npm/chart.js\"></script></html>",
        }}
        onClose={() => {}}
      />,
    );
    expect(screen.getByTestId("artifact-cdn-blocked")).toBeTruthy();
  });

  it("downloads the source as a named file", () => {
    const clicks = [];
    const origCreate = URL.createObjectURL;
    const origRevoke = URL.revokeObjectURL;
    const origClick = HTMLAnchorElement.prototype.click;
    URL.createObjectURL = () => "blob:anila-test";
    URL.revokeObjectURL = () => {};
    HTMLAnchorElement.prototype.click = function click() {
      clicks.push({ download: this.download, href: this.href });
    };
    try {
      render(
        <ArtifactPanel
          artifact={{ kind: "svg", source: DONUT_SVG }}
          classified={false}
          onClose={() => {}}
        />,
      );
      fireEvent.click(screen.getByTestId("artifact-download"));
      expect(clicks).toHaveLength(1);
      expect(clicks[0].download).toBe("anila-artifact.svg");
      const result = downloadArtifactSource("# hi", "markdown");
      expect(result).toEqual({ filename: "anila-artifact.md", mime: "text/markdown" });
      expect(clicks[1].download).toBe("anila-artifact.md");
    } finally {
      URL.createObjectURL = origCreate;
      URL.revokeObjectURL = origRevoke;
      HTMLAnchorElement.prototype.click = origClick;
    }
  });

  it("does not warn after Three.js CDN is rewritten to the vendor", () => {
    render(
      <ArtifactPanel
        artifact={{
          kind: "html",
          source:
            "<!DOCTYPE html><html><script src=\"https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js\"></script></html>",
        }}
        onClose={() => {}}
      />,
    );
    expect(screen.queryByTestId("artifact-cdn-blocked")).toBeNull();
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

  it("gives long code its own scroll box so the conversation can scroll separately", () => {
    render(
      <ArtifactPreviewProvider onOpen={() => {}}>
        <MarkdownView text={"```html\n<!DOCTYPE html><html><body>x</body></html>\n```"} />
      </ArtifactPreviewProvider>,
    );
    const pre = screen.getByTestId("md-code-pre");
    expect(pre.style.maxHeight).toMatch(/60vh/);
    expect(pre.style.overflow).toBe("auto");
    expect(pre.style.overscrollBehavior).toBe("contain");
  });

  it("shows 預覽 on a jsx-fenced React component", () => {
    const opened = [];
    const md = "```jsx\nfunction App() {\n  return (\n    <h1>太陽系</h1>\n  );\n}\n```";
    render(
      <ArtifactPreviewProvider onOpen={(a) => opened.push(a)}>
        <MarkdownView text={md} />
      </ArtifactPreviewProvider>,
    );
    fireEvent.click(screen.getByTestId("artifact-preview-btn"));
    expect(opened).toHaveLength(1);
    expect(opened[0].kind).toBe("jsx");
    expect(opened[0].source).toContain("function App");
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

describe("ArtifactPanel 改這一段", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  function mockSourceSelection(pre, text) {
    vi.spyOn(window, "getSelection").mockReturnValue({
      isCollapsed: false,
      toString: () => text,
      anchorNode: pre,
      focusNode: pre,
      rangeCount: 1,
      getRangeAt: () => ({ commonAncestorContainer: pre }),
    });
    fireEvent.mouseUp(pre);
  }

  it("denies revise on classified chats with the same copy as copy", () => {
    render(
      <ArtifactPanel
        artifact={{ kind: "svg", source: DONUT_SVG }}
        classified={true}
        classificationLevel="密"
        onClose={() => {}}
      />,
    );
    const denied = screen.getByTestId("artifact-revise-denied");
    expect(denied).toBeDisabled();
    expect(denied.getAttribute("title")).toBe(classifiedCopyDenial("密"));
    expect(screen.queryByTestId("artifact-revise")).toBeNull();
  });

  it("does not send when nothing is selected", () => {
    const onRevise = vi.fn();
    render(
      <ArtifactPanel
        artifact={{ kind: "markdown", source: "# hi" }}
        classified={false}
        onClose={() => {}}
        onRevise={onRevise}
      />,
    );
    fireEvent.click(screen.getByTestId("artifact-tab-source"));
    fireEvent.click(screen.getByTestId("artifact-revise"));
    expect(screen.getByTestId("artifact-revise-hint").textContent).toMatch(/選一段/);
    expect(onRevise).not.toHaveBeenCalled();
  });

  it("sends a revise prompt after selection and instruction", () => {
    const onRevise = vi.fn();
    render(
      <ArtifactPanel
        artifact={{ kind: "html", language: "html", source: "<h1>舊</h1>" }}
        classified={false}
        onClose={() => {}}
        onRevise={onRevise}
      />,
    );
    fireEvent.click(screen.getByTestId("artifact-tab-source"));
    mockSourceSelection(screen.getByTestId("artifact-source"), "<h1>舊</h1>");
    fireEvent.click(screen.getByTestId("artifact-revise"));
    fireEvent.change(screen.getByTestId("artifact-revise-input"), {
      target: { value: "標題改成新" },
    });
    fireEvent.click(screen.getByTestId("artifact-revise-send"));
    expect(onRevise).toHaveBeenCalledTimes(1);
    const prompt = onRevise.mock.calls[0][0];
    expect(prompt).toContain("標題改成新");
    expect(prompt).toContain("<h1>舊</h1>");
    expect(onRevise.mock.calls[0][1]).toEqual({ kind: "html" });
  });

  it("keeps indentation when the source selection has leading spaces", () => {
    const onRevise = vi.fn();
    const source = "<div>\n  <p>x</p>\n</div>";
    render(
      <ArtifactPanel
        artifact={{ kind: "html", language: "html", source }}
        classified={false}
        onClose={() => {}}
        onRevise={onRevise}
      />,
    );
    fireEvent.click(screen.getByTestId("artifact-tab-source"));
    mockSourceSelection(screen.getByTestId("artifact-source"), "  <p>x</p>");
    fireEvent.click(screen.getByTestId("artifact-revise"));
    fireEvent.change(screen.getByTestId("artifact-revise-input"), {
      target: { value: "改成 y" },
    });
    fireEvent.click(screen.getByTestId("artifact-revise-send"));
    expect(onRevise.mock.calls[0][0]).toContain("  <p>x</p>");
  });

  it("does not capture a selection that crosses outside the source pre", () => {
    const onRevise = vi.fn();
    render(
      <ArtifactPanel
        artifact={{ kind: "html", language: "html", source: "<h1>舊</h1>" }}
        classified={false}
        onClose={() => {}}
        onRevise={onRevise}
      />,
    );
    fireEvent.click(screen.getByTestId("artifact-tab-source"));
    const pre = screen.getByTestId("artifact-source");
    const outside = document.createElement("span");
    outside.textContent = "OUT";
    pre.parentElement.appendChild(outside);
    const range = document.createRange();
    const preText = pre.firstChild;
    range.setStart(preText, 0);
    range.setEnd(outside.firstChild, 3);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    fireEvent.mouseUp(pre);
    fireEvent.click(screen.getByTestId("artifact-revise"));
    expect(screen.getByTestId("artifact-revise-hint").textContent).toMatch(/選一段/);
    expect(onRevise).not.toHaveBeenCalled();
  });

  it("hints and does not send if the source selection is cleared after the box opens", () => {
    const onRevise = vi.fn();
    render(
      <ArtifactPanel
        artifact={{ kind: "html", language: "html", source: "<h1>舊</h1>" }}
        classified={false}
        onClose={() => {}}
        onRevise={onRevise}
      />,
    );
    fireEvent.click(screen.getByTestId("artifact-tab-source"));
    mockSourceSelection(screen.getByTestId("artifact-source"), "<h1>舊</h1>");
    fireEvent.click(screen.getByTestId("artifact-revise"));
    expect(screen.getByTestId("artifact-revise-input")).toBeTruthy();
    vi.spyOn(window, "getSelection").mockReturnValue({
      isCollapsed: true,
      toString: () => "",
      anchorNode: null,
      focusNode: null,
      rangeCount: 0,
      getRangeAt: () => ({ commonAncestorContainer: null }),
    });
    fireEvent.mouseUp(screen.getByTestId("artifact-source"));
    fireEvent.change(screen.getByTestId("artifact-revise-input"), {
      target: { value: "標題改成新" },
    });
    fireEvent.click(screen.getByTestId("artifact-revise-send"));
    expect(screen.getByTestId("artifact-revise-hint").textContent).toMatch(/選一段/);
    expect(onRevise).not.toHaveBeenCalled();
  });
});

describe("產物修訂後預覽跟著換", () => {
  it("replaces the open artifact from the ticket message when the new fence is not a prefix", () => {
    const opened = [];
    const current = { kind: "html", source: "<h1>舊</h1>" };
    render(
      <ArtifactPreviewProvider
        artifact={current}
        onOpen={(a) => opened.push(a)}
        pendingRevision={{ messageId: "a-new", kind: "html" }}
      >
        <MarkdownView
          messageId="a-new"
          streaming={false}
          text={"```html\n<h1>新</h1>\n```"}
        />
      </ArtifactPreviewProvider>,
    );
    expect(opened.some((a) => a.source.includes("<h1>新</h1>"))).toBe(true);
  });

  it("does not replace from a historical fence while a ticket is pending", () => {
    const opened = [];
    const current = { kind: "html", source: "<h1>舊</h1>" };
    render(
      <ArtifactPreviewProvider
        artifact={current}
        onOpen={(a) => opened.push(a)}
        pendingRevision={{ messageId: "a-new", kind: "html" }}
      >
        <MarkdownView
          messageId="a-old"
          streaming={false}
          text={"```html\n<h1>歷史</h1>\n```"}
        />
      </ArtifactPreviewProvider>,
    );
    expect(opened.some((a) => a.source.includes("歷史"))).toBe(false);
  });

  it("prefers the longer same-kind fence on the ticket message", () => {
    const opened = [];
    const current = { kind: "html", source: "<h1>舊</h1>" };
    render(
      <ArtifactPreviewProvider
        artifact={current}
        onOpen={(a) => opened.push(a)}
        pendingRevision={{ messageId: "a-new", kind: "html" }}
      >
        <MarkdownView
          messageId="a-new"
          streaming={false}
          text={"```html\n<p>短</p>\n```\n\n```html\n<h1>完整新產物</h1>\n```"}
        />
      </ArtifactPreviewProvider>,
    );
    expect(opened.some((a) => a.source.includes("完整新產物"))).toBe(true);
  });

  it("does not replace the open artifact when the ticket turn was stopped", () => {
    const opened = [];
    const current = { kind: "html", source: "<h1>舊</h1>" };
    render(
      <ArtifactPreviewProvider
        artifact={current}
        onOpen={(a) => opened.push(a)}
        pendingRevision={{ messageId: "a-new", kind: "html" }}
      >
        <MarkdownView
          messageId="a-new"
          streaming={false}
          streamState="stopped"
          text={"```html\n<h1>半截</h1>\n```"}
        />
      </ArtifactPreviewProvider>,
    );
    expect(opened.some((a) => a.source.includes("半截"))).toBe(false);
  });

  it("replaces markdown from the raw reply so nested ``` is not truncated", () => {
    const opened = [];
    const current = { kind: "markdown", source: "# 舊" };
    const text = [
      "```markdown",
      "# 範例",
      "",
      "```js",
      "alert(1)",
      "```",
      "```",
    ].join("\n");
    render(
      <ArtifactPreviewProvider
        artifact={current}
        onOpen={(a) => opened.push(a)}
        pendingRevision={{ messageId: "a-new", kind: "markdown" }}
      >
        <MarkdownView
          messageId="a-new"
          streaming={false}
          streamState="complete"
          text={text}
        />
      </ArtifactPreviewProvider>,
    );
    const hit = opened.find((a) => a.kind === "markdown");
    expect(hit?.source).toContain("```js");
    expect(hit?.source).toContain("alert(1)");
    expect(hit?.source).toContain("# 範例");
  });

  it("does not replace from a ticket belonging to another conversation", () => {
    const opened = [];
    const current = { kind: "html", source: "<h1>舊</h1>" };
    render(
      <ArtifactPreviewProvider
        artifact={current}
        onOpen={(a) => opened.push(a)}
        pendingRevision={{ messageId: "a-new", kind: "html", conversationId: 5 }}
      >
        <MarkdownView
          messageId="a-new"
          conversationId={9}
          streaming={false}
          streamState="complete"
          text={"```html\n<h1>新</h1>\n```"}
        />
      </ArtifactPreviewProvider>,
    );
    expect(opened.some((a) => a.source.includes("<h1>新</h1>"))).toBe(false);
  });
});
