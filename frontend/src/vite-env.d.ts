/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** ブラウザ単独起動(Electronなし)時の開発用フォールバック(#78)。 */
  readonly VITE_DEV_BACKEND_URL?: string;
  readonly VITE_DEV_BACKEND_TOKEN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
