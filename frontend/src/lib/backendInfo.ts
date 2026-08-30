import type { BackendInfo } from "./electron-api";

let cached: Promise<BackendInfo> | null = null;

/**
 * #78: `window.api.getBackendInfo()` があればそれを使う(Electron 経由)。
 * 無ければブラウザ単独起動時の開発用フォールバックを使う(#15 完了条件)。
 */
export function getBackendInfo(): Promise<BackendInfo> {
  if (cached) return cached;
  cached = window.api
    ? window.api.getBackendInfo()
    : Promise.resolve({
        baseUrl: import.meta.env.VITE_DEV_BACKEND_URL ?? "http://127.0.0.1:8000",
        token: import.meta.env.VITE_DEV_BACKEND_TOKEN ?? "",
      });
  return cached;
}
