import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  // #77: 本番ビルドは Electron から file:// で index.html を読み込むため、
  // アセットパスは絶対(/assets/..)ではなく相対(./assets/..)にする。
  base: "./",
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "dist",
  },
  server: {
    port: 5173,
    strictPort: true,
  },
});
