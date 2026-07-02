/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_CSP_BACKEND?: string
  readonly VITE_DEFAULT_CHAT_MODEL?: string
  /** Base URL for the anila-studio service (`/api/studio/*`). Empty string = use vite proxy / same-origin nginx. */
  readonly VITE_STUDIO_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
