/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_CSP_BACKEND?: string
  /** Base URL for anila-studio. Empty string = Vite proxy / same-origin nginx (the five Studio prefixes). */
  readonly VITE_STUDIO_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
