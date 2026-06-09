import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  // Served same-origin under /app (nginx strips the prefix); base makes
  // built asset URLs absolute under /app/.
  base: "/app/",
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: "./vitest.setup.js",
  },
});
