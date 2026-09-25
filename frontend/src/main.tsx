import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./index.css";
import { initQuantizeSettings } from "./stores/quantizeStore";
import { initScoreView } from "./stores/scoreViewStore";
import { initTheme } from "./stores/themeStore";

// #152: 描画前にテーマ(既定=ダーク)を適用し、起動直後のちらつきを避ける。
initTheme();
// #172: 先頭N小節プレビューの表示小節数も描画前に読み込む。
initScoreView();
// #174: 量子化の適用設定(最小音符単位・強さ・入切)も同じく読み込む。
initQuantizeSettings();

const queryClient = new QueryClient();

const rootElement = document.getElementById("root");
if (!rootElement) throw new Error("#root element not found");

createRoot(rootElement).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
