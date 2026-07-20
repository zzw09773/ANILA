import { readFile } from "node:fs/promises";

const css = await readFile(new URL("../src/tokens.css", import.meta.url), "utf8");
const required = [
  "--anila-color-accent",
  "--anila-color-bg",
  "--anila-color-fg",
  "--anila-font-sans",
  "--anila-font-mono",
  "--anila-radius-md",
];

for (const token of required) {
  if (!css.includes(`${token}:`)) throw new Error(`Missing required token: ${token}`);
}
if (/@import\s|url\s*\(/i.test(css)) {
  throw new Error("Framework-neutral tokens must remain self-contained and air-gap safe");
}
console.log(`Verified ${required.length} required design tokens`);
