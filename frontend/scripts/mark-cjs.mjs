// ルートの package.json は "type": "module" だが、electron/ は tsconfig.electron.json で
// CommonJS にコンパイルしている(preload はCommonJS前提のため)。Node/Electron は拡張子 .js を
// 最も近い package.json の "type" で解釈するため、dist-electron/ 配下だけ
// "type": "commonjs" を明示するマーカーを置く。
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const distElectronDir = path.resolve(scriptDir, "..", "dist-electron");

mkdirSync(distElectronDir, { recursive: true });
writeFileSync(path.join(distElectronDir, "package.json"), `${JSON.stringify({ type: "commonjs" })}\n`);
