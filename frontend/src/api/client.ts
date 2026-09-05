import { getBackendInfo } from "../lib/backendInfo";
import type { components } from "./generated";

export type Project = components["schemas"]["Project"];
export type Job = components["schemas"]["Job"];
export type PeaksResponse = components["schemas"]["PeaksResponse"];
export type Beatmap = components["schemas"]["Beatmap"];
export type BeatmapEditRequest = components["schemas"]["BeatmapEditRequest"];
export type SeparationPreset = "fast" | "standard" | "high_quality";
export type ExecutionProvider = "auto" | "cpu" | "directml";

/**
 * `allowNotFound: true` の場合、404 を例外ではなく `null` として扱う
 * (まだ実行されていないステージの結果取得用)。呼び出しごとに
 * `getBackendInfo`/認証ヘッダ付与/エラーメッセージ整形を重複実装しないよう、
 * オプション経由で1箇所に集約する(#21-M1レビュー指摘)。
 */
async function apiFetch(
  path: string,
  init?: RequestInit & { allowNotFound?: false },
): Promise<Response>;
async function apiFetch(
  path: string,
  init: RequestInit & { allowNotFound: true },
): Promise<Response | null>;
async function apiFetch(
  path: string,
  init: RequestInit & { allowNotFound?: boolean } = {},
): Promise<Response | null> {
  const { allowNotFound, ...requestInit } = init;
  const { baseUrl, token } = await getBackendInfo();
  const headers = new Headers(requestInit.headers);
  if (token) headers.set("X-AME-Token", token);
  const resp = await fetch(`${baseUrl}${path}`, { ...requestInit, headers });
  if (allowNotFound && resp.status === 404) return null;
  if (!resp.ok) {
    const body = await resp.text().catch(() => "");
    throw new Error(`${requestInit.method ?? "GET"} ${path} -> ${resp.status}: ${body}`);
  }
  return resp;
}

/**
 * 認証ヘッダ付きで音声を取得し、objectURL化して返す(#21)。
 * `<audio>`/wavesurfer.js は自前で fetch するため、トークン付きヘッダを渡せない。
 * 呼び出し元は再生終了後に `URL.revokeObjectURL` で解放すること。
 */
export async function fetchAudioObjectUrl(path: string): Promise<string> {
  const resp = await apiFetch(path);
  const blob = await resp.blob();
  return URL.createObjectURL(blob);
}

export async function listProjects(): Promise<Project[]> {
  const resp = await apiFetch("/api/projects");
  const data = (await resp.json()) as { projects: Project[] };
  return data.projects;
}

export async function createProject(file: File): Promise<Project> {
  const form = new FormData();
  form.append("file", file);
  const resp = await apiFetch("/api/projects", { method: "POST", body: form });
  return (await resp.json()) as Project;
}

export async function getProject(projectId: string): Promise<Project> {
  const resp = await apiFetch(`/api/projects/${projectId}`);
  return (await resp.json()) as Project;
}

export async function deleteProject(projectId: string): Promise<void> {
  await apiFetch(`/api/projects/${projectId}`, { method: "DELETE" });
}

export async function getOriginalAudioUrl(projectId: string): Promise<string> {
  const { baseUrl, token } = await getBackendInfo();
  // wavesurfer.js は独自に fetch するため、トークンを付けたヘッダを渡せるよう
  // fetchParams を使う側(AudioPlayer)で組み立てる。ここでは base の情報のみ返す。
  void token;
  return `${baseUrl}/api/projects/${projectId}/audio/original`;
}

async function runStage(
  projectId: string,
  stage: string,
  params: Record<string, unknown> = {},
): Promise<{ job_id: string }> {
  const resp = await apiFetch(`/api/projects/${projectId}/stages/${stage}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ params }),
  });
  return (await resp.json()) as { job_id: string };
}

export async function runDummyStage(
  projectId: string,
  params: Record<string, unknown> = {},
): Promise<{ job_id: string }> {
  return runStage(projectId, "dummy", params);
}

/** #16: Stage 1 音源分離を実行する。 */
export async function runSeparateStage(
  projectId: string,
  preset: SeparationPreset,
  executionProvider: ExecutionProvider = "auto",
): Promise<{ job_id: string }> {
  return runStage(projectId, "separate", { preset, execution_provider: executionProvider });
}

/** #18: Stage 2 ビート・拍子推定を実行する。 */
export async function runBeatStage(projectId: string): Promise<{ job_id: string }> {
  return runStage(projectId, "beat", {});
}

/** #21 TrackList: 分離済みステム名の一覧(分離未実行なら空配列)。 */
export async function listStems(projectId: string): Promise<string[]> {
  const resp = await apiFetch(`/api/projects/${projectId}/stems`);
  const data = (await resp.json()) as { names: string[] };
  return data.names;
}

export async function getStemAudioUrl(projectId: string, name: string): Promise<string> {
  return fetchAudioObjectUrl(`/api/projects/${projectId}/audio/stems/${name}`);
}

/** #21: 波形ピークデータ。`name` は "original" またはステム名。 */
export async function getPeaks(projectId: string, name: string): Promise<PeaksResponse> {
  const resp = await apiFetch(`/api/projects/${projectId}/analysis/peaks/${name}`);
  return (await resp.json()) as PeaksResponse;
}

/** #18: beatmap.json。ビート推定が未実行なら `null`。 */
export async function getBeatmap(projectId: string): Promise<Beatmap | null> {
  const resp = await apiFetch(`/api/projects/${projectId}/analysis/beatmap`, {
    allowNotFound: true,
  });
  if (!resp) return null;
  return (await resp.json()) as Beatmap;
}

/**
 * #20 BeatGridEditor: 手動補正を適用する。
 *
 * `Partial<BeatmapEditRequest>` を受け取る(生成された `BeatmapEditRequest` 型は
 * `rotate_downbeat` を必須にしているが、これはOpenAPIスキーマ側で`@default false`を
 * 持つフィールドをopenapi-typescriptが必須として扱うためで、バックエンドは実際には
 * JSONで省略されたフィールドをPydanticの既定値で補う)。呼び出し側は補正したい
 * フィールドだけを指定すればよい。
 */
export async function patchBeatmap(
  projectId: string,
  body: Partial<BeatmapEditRequest>,
): Promise<Beatmap> {
  const resp = await apiFetch(`/api/projects/${projectId}/analysis/beatmap`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return (await resp.json()) as Beatmap;
}

export async function getJob(jobId: string): Promise<Job> {
  const resp = await apiFetch(`/api/jobs/${jobId}`);
  return (await resp.json()) as Job;
}

export async function cancelJob(jobId: string): Promise<void> {
  await apiFetch(`/api/jobs/${jobId}/cancel`, { method: "POST" });
}
