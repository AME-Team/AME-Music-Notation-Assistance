// #62/#80(Q-17): 配布用に「embeddable Python + 事前構築したvenv」を組み立てる。
//
// 決定事項(Issue #80): PyInstallerの単一exe化はonnxruntime/torch/librosa等の
// 動的import解決が難しく実行時クラッシュのリスクが高いため不採用とし、
// (b) embeddable Python + 事前構築したvenv同梱を採用した。モデルファイル
// (demucs-onnx/beat-this/piano_transcription_inference/Basic Pitch)は各ライブラリ/
// 実装が既に持つ初回起動時ダウンロード+キャッシュ機構(huggingface_hub/torch.hub/
// 独自のensure_*)をそのまま使うため、ここでは扱わない。
//
// 生成物(`frontend/python-runtime/`, `frontend/ffmpeg/`)は`electron-builder.yml`の
// `extraResources`でパッケージへ同梱される。両ディレクトリとも巨大な
// Windows専用バイナリを含むためリポジトリには含めない(.gitignore参照)。
//
// Windows専用(NFR-08′)。本スクリプト自体もWindows上でのみ実行できる
// (embeddable Pythonの実体がWindowsバイナリのため、生成した`python.exe`で
// pip installを実行する必要がある)。

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  cpSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const frontendDir = path.resolve(scriptDir, "..");
const backendDir = path.resolve(frontendDir, "..", "backend");
const runtimeDir = path.join(frontendDir, "python-runtime");
const ffmpegDir = path.join(frontendDir, "ffmpeg");
const tmpDir = path.join(frontendDir, ".python-runtime-tmp");

// バックエンドの`requires-python = ">=3.12,<3.13"`(pyproject.toml)に合わせた
// 固定バージョン。embeddable Pythonのマイナーバージョンが一致していないと
// pipでインストールした3.12向けwheelが読み込めない。
const PYTHON_VERSION = "3.12.8";
const PYTHON_EMBED_URL = `https://www.python.org/ftp/python/${PYTHON_VERSION}/python-${PYTHON_VERSION}-embed-amd64.zip`;
// python.orgが公式に配布する`<file>.spdx.json`(SPDXパッケージ目録)内の
// "CPython"パッケージのSHA256チェックサム(get-pip/ffmpegと同じ「固定バージョン+
// ハッシュ検証」方針、#62 Gate1レビュー指摘)。`PYTHON_VERSION`を変更する際は
// `https://www.python.org/ftp/python/<version>/python-<version>-embed-amd64.zip.spdx.json`
// を参照して値も更新すること。
const PYTHON_EMBED_SHA256 = "8d3f33be9eb810f23c102f08475af2854e50484b8e4e06275e937be61ce3d2fb";

// get-pip.py(bootstrap.pypa.io)は"latest"の可動URLだが、`pypa/get-pip`リポジトリの
// 特定コミットへ固定したraw URLとSHA256でダウンロードを検証することで、
// ffmpeg・ONNXモデル(#56の`_model_fetch.fetch_verified_model`)と同じ
// 「タグ/コミット固定+ハッシュ検証」方針に統一する(#62 Gate1レビュー指摘)。
const GET_PIP_COMMIT = "af54dfe793b24685f8dc4ebba0630d9f2d77653c";
const GET_PIP_URL = `https://raw.githubusercontent.com/pypa/get-pip/${GET_PIP_COMMIT}/public/get-pip.py`;
const GET_PIP_SHA256 = "fb24e693bab954209a063d90953621412ccad4a500905a726286e038f508ddf6";

// LGPLビルド(GPLビルドはコピーレフト義務を避けるため使わない)。BtbNの
// リリースは"latest"エイリアスの中身がビルドごとに更新されうる(かつ
// ffmpegのマイナーバージョン自体も定期的に上がる)ため、アセット名を
// 完全固定にはせず「win64のLGPL単体(shared版は除く)ビルド」に一致する
// ものをリリース内から動的に選ぶ(#62 Gate1レビュー指摘: `n9.0`のような
// バージョン文字列を固定すると、上流がn9.1等へ更新した時点でアセットが
// 見つからずビルドが必ず失敗していた)。ダウンロード内容自体は、リリースに
// 同梱される`checksums.sha256`を都度取得しその場で検証する(#56の
// `_model_fetch.fetch_verified_model`と同種の考え方)。
const FFMPEG_RELEASE_API = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest";
const FFMPEG_ASSET_PATTERN = /^ffmpeg-n[\d.]+-latest-win64-lgpl-[\d.]+\.zip$/;

function log(message) {
  console.log(`[prepare-python-runtime] ${message}`);
}

function requireWindows() {
  if (process.platform !== "win32") {
    throw new Error(
      "prepare-python-runtime.mjs はWindows専用です(embeddable PythonはWindows " +
        "バイナリのため、生成したpython.exeでのpip installをWindows上で実行する " +
        "必要があります)。Windows環境(またはwindows-latest CIランナー)で実行して" +
        "ください。",
    );
  }
}

function runPowerShell(command) {
  execFileSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", command], {
    stdio: "inherit",
  });
}

async function download(url, destPath, { expectedSha256 } = {}) {
  log(`downloading ${url}`);
  const resp = await fetch(url, { redirect: "follow" });
  if (!resp.ok) {
    throw new Error(`failed to download ${url}: HTTP ${resp.status}`);
  }
  const buffer = Buffer.from(await resp.arrayBuffer());
  if (expectedSha256) {
    const actualSha256 = sha256Of(buffer);
    if (actualSha256 !== expectedSha256.toLowerCase()) {
      throw new Error(
        `checksum mismatch for ${url} (got ${actualSha256}, expected ${expectedSha256}); ` +
          "download likely corrupted",
      );
    }
  }
  writeFileSync(destPath, buffer);
  return buffer;
}

function sha256Of(buffer) {
  return createHash("sha256").update(buffer).digest("hex");
}

function extractZip(zipPath, destDir) {
  mkdirSync(destDir, { recursive: true });
  runPowerShell(
    `Expand-Archive -LiteralPath '${zipPath}' -DestinationPath '${destDir}' -Force`,
  );
}

/** embeddable Pythonは既定でsite-packagesを無効化しているため、`._pth`ファイルの
 * `#import site`を有効化する(pip/インストール済みライブラリを読み込むために必須)。
 *
 * `import site`を有効化するだけでなく、`Lib\site-packages`を`._pth`へ明示的に
 * 追記する。embeddable配布は通常のインストールと`sys.prefix`の解決が異なり、
 * `site.main()`による自動検出だけでは`Lib\site-packages`が`sys.path`に載らない
 * ことがある、というembeddable Python特有の既知の落とし穴(コミュニティで
 * 広く報告されている)への対策。
 */
function enableSitePackages(pythonDir) {
  const pthPath = path.join(pythonDir, `python${PYTHON_VERSION.split(".").slice(0, 2).join("")}._pth`);
  if (!existsSync(pthPath)) {
    throw new Error(`expected ._pth file not found: ${pthPath}`);
  }
  const content = readFileSync(pthPath, "utf-8");
  let patched = content.replace(/^#\s*import site/m, "import site");
  if (patched === content && !content.includes("\nimport site")) {
    throw new Error(
      `could not enable 'import site' in ${pthPath}; unexpected file contents`,
    );
  }
  if (!patched.includes("Lib\\site-packages")) {
    patched = `Lib\\site-packages\n${patched}`;
  }
  writeFileSync(pthPath, patched, "utf-8");
  log(`enabled site-packages in ${pthPath}`);
}

async function setupPython() {
  log(`setting up embeddable Python ${PYTHON_VERSION}...`);
  rmSync(runtimeDir, { recursive: true, force: true });
  mkdirSync(runtimeDir, { recursive: true });
  mkdirSync(tmpDir, { recursive: true });

  const zipPath = path.join(tmpDir, "python-embed.zip");
  await download(PYTHON_EMBED_URL, zipPath, { expectedSha256: PYTHON_EMBED_SHA256 });
  extractZip(zipPath, runtimeDir);
  enableSitePackages(runtimeDir);

  const pythonExe = path.join(runtimeDir, "python.exe");
  const getPipPath = path.join(tmpDir, "get-pip.py");
  await download(GET_PIP_URL, getPipPath, { expectedSha256: GET_PIP_SHA256 });
  log("installing pip...");
  execFileSync(pythonExe, [getPipPath, "--no-warn-script-location"], { stdio: "inherit" });

  log("exporting backend dependency list via uv...");
  const requirementsPath = path.join(tmpDir, "requirements.txt");
  execFileSync(
    "uv",
    [
      "export",
      "--format",
      "requirements.txt",
      "--emit-index-url",
      // バックエンド自体(`ame-backend`)は`pyproject.toml`で`[tool.uv] package = false`の
      // ため通常は出力されないが、将来設定が変わっても壊れないよう明示的に除外する
      // (#62 Gate1レビュー指摘)。ハッシュは`--no-hashes`を付けずデフォルトのまま
      // 出力し、pipのハッシュ検証(依存パッケージの完全性チェック)を有効に保つ。
      "--no-emit-project",
      "-o",
      requirementsPath,
    ],
    { cwd: backendDir, stdio: "inherit" },
  );

  log("installing backend dependencies into the embedded runtime (this can take a while)...");
  execFileSync(
    pythonExe,
    ["-m", "pip", "install", "--no-warn-script-location", "-r", requirementsPath],
    { stdio: "inherit" },
  );

  log("copying backend application source...");
  cpSync(path.join(backendDir, "app"), path.join(runtimeDir, "app"), { recursive: true });

  log(`embeddable Python runtime ready at ${runtimeDir}`);
}

async function setupFfmpeg() {
  log("setting up ffmpeg...");
  rmSync(ffmpegDir, { recursive: true, force: true });
  mkdirSync(ffmpegDir, { recursive: true });

  const releaseResp = await fetch(FFMPEG_RELEASE_API);
  if (!releaseResp.ok) {
    throw new Error(`failed to query FFmpeg-Builds release info: HTTP ${releaseResp.status}`);
  }
  const release = await releaseResp.json();
  const asset = release.assets.find((a) => FFMPEG_ASSET_PATTERN.test(a.name));
  const checksumsAsset = release.assets.find((a) => a.name === "checksums.sha256");
  if (!asset || !checksumsAsset) {
    throw new Error(
      "expected assets not found in BtbN/FFmpeg-Builds latest release (looked for an " +
        `asset matching ${FFMPEG_ASSET_PATTERN} and checksums.sha256); the release ` +
        "layout may have changed.",
    );
  }

  const checksumsText = Buffer.from(
    await (await fetch(checksumsAsset.browser_download_url)).arrayBuffer(),
  ).toString("utf-8");
  const checksumLine = checksumsText.split("\n").find((line) => line.trim().endsWith(asset.name));
  if (!checksumLine) {
    throw new Error(`no checksum entry found for ${asset.name} in checksums.sha256`);
  }
  const expectedSha256 = checksumLine.trim().split(/\s+/)[0].toLowerCase();

  const zipPath = path.join(tmpDir, "ffmpeg.zip");
  await download(asset.browser_download_url, zipPath, { expectedSha256 });

  const extractDir = path.join(tmpDir, "ffmpeg-extracted");
  extractZip(zipPath, extractDir);
  // BtbNのzipは `<アセット名の拡張子無し>/bin/{ffmpeg,ffprobe}.exe` という
  // 入れ子構造を持つ。実行ファイルだけをffmpegDir直下へ引き上げる。
  const nestedRoot = path.join(extractDir, path.basename(asset.name, ".zip"));
  const nestedBin = path.join(nestedRoot, "bin");
  for (const exe of ["ffmpeg.exe", "ffprobe.exe"]) {
    renameSync(path.join(nestedBin, exe), path.join(ffmpegDir, exe));
  }
  // LGPLビルドを配布するにはライセンス全文の同梱・入手元の明示が必要
  // (#62 Gate1レビュー指摘)。zip同梱の`LICENSE.txt`(LGPL条項の一覧を含む)を
  // そのままffmpegDirへコピーする。
  cpSync(path.join(nestedRoot, "LICENSE.txt"), path.join(ffmpegDir, "LICENSE.txt"));

  log(`ffmpeg (${asset.name}) ready at ${ffmpegDir} (verified sha256=${expectedSha256})`);
}

async function main() {
  requireWindows();
  try {
    await setupPython();
    await setupFfmpeg();
  } finally {
    rmSync(tmpDir, { recursive: true, force: true });
  }
  log("done.");
}

main().catch((err) => {
  console.error(`[prepare-python-runtime] failed: ${err.message}`);
  process.exit(1);
});
