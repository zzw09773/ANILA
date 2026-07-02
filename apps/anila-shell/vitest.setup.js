import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// Sprint 13 PR B2: ensure RTL unmounts components between tests so the
// jsdom body doesn't accumulate siblings (which trips getByRole's
// "found multiple elements" guard).
afterEach(() => {
  cleanup();
});
