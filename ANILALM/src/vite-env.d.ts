/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_CSP_BACKEND?: string
  readonly VITE_DEFAULT_CHAT_MODEL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
