// COMPOSER_FILE_ACCEPT ⊆ CSP ALLOWED_EXTENSIONS — derived, not hard-coded.
//
// If the picker ever offers an extension the backend rejects, the paperclip
// dialog lies to the user (they pick a file, upload fails). Parse both lists
// from source so the next divergence fails the suite instead of shipping.

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { COMPOSER_FILE_ACCEPT } from "../chat.jsx";

const REPO_ROOT = resolve(process.cwd(), "../..");
const ATTACHMENT_SERVICE = resolve(
  REPO_ROOT,
  "services/csp/app/services/attachment_service.py",
);

/** Parse ALLOWED_EXTENSIONS = { ... } from attachment_service.py. */
export function parseAllowedExtensions(pySource) {
  const start = pySource.indexOf("ALLOWED_EXTENSIONS");
  if (start < 0) throw new Error("ALLOWED_EXTENSIONS not found");
  const brace = pySource.indexOf("{", start);
  const end = pySource.indexOf("}", brace);
  if (brace < 0 || end < 0) throw new Error("ALLOWED_EXTENSIONS set incomplete");
  const body = pySource.slice(brace, end + 1);
  const exts = new Set();
  for (const m of body.matchAll(/"(\.[A-Za-z0-9]+)"/g)) {
    exts.add(m[1].toLowerCase());
  }
  if (exts.size === 0) throw new Error("ALLOWED_EXTENSIONS parsed empty");
  return exts;
}

/** Extensions (and MIME tokens) offered by the composer picker. */
export function parsePickerTokens(accept) {
  return accept
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

export function pickerExtensions(accept) {
  return parsePickerTokens(accept)
    .filter((t) => t.startsWith("."))
    .map((t) => t.toLowerCase());
}

describe("COMPOSER_FILE_ACCEPT ⊆ ALLOWED_EXTENSIONS", () => {
  const py = readFileSync(ATTACHMENT_SERVICE, "utf8");
  const allowed = parseAllowedExtensions(py);
  const offered = pickerExtensions(COMPOSER_FILE_ACCEPT);

  it("derives a non-empty ALLOWED_EXTENSIONS from the backend source", () => {
    expect(allowed.size).toBeGreaterThan(10);
    expect(allowed.has(".dcm")).toBe(true);
    expect(allowed.has(".out")).toBe(true);
    expect(allowed.has(".svg")).toBe(true); // image-only asymmetry, still allowed
  });

  it("every extension the picker offers is accepted by the backend", () => {
    const extras = offered.filter((ext) => !allowed.has(ext));
    expect(extras).toEqual([]);
  });

  it("offers DATCOM / owner file types .dcm and .out", () => {
    expect(offered).toContain(".dcm");
    expect(offered).toContain(".out");
  });

  it("keeps image/* and still lists .svg (image-only; no text parser)", () => {
    expect(parsePickerTokens(COMPOSER_FILE_ACCEPT)).toContain("image/*");
    expect(offered).toContain(".svg");
  });

  it("fails when the picker offers a backend-rejected extension (mutation check)", () => {
    const bogus = COMPOSER_FILE_ACCEPT + ",.not-a-real-ext";
    const extras = pickerExtensions(bogus).filter((ext) => !allowed.has(ext));
    expect(extras).toContain(".not-a-real-ext");
  });
});
