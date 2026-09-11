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
 * Score IR(#23, 設計書§10.2)。`GET /score`/`POST /score/ops`はバックエンド側で
 * `response_model`を素の`dict`にしている(`api/score.py`のdocstring参照:
 * `domain.score.ScoreIR`をそのまま使うとOpenAPIコンポーネント名が衝突するため)。
 * そのため`generated.ts`に対応する型が無く、ここで`domain/score.py`のJSON形状を
 * 手書きする(このエンドポイント2つに限った、意図的な例外)。
 */
export interface ScoreSpelling {
  step: string;
  alter: number | null;
  octave: number;
}

export type NoteProvenance = "amt" | "baseline" | "llm" | "agent" | "user";
export type NoteStatus = "active" | "deleted" | "muted";

export interface ScoreNote {
  id: number;
  onset_sec: number;
  duration_sec: number;
  onset_tick: number | null;
  duration_tick: number | null;
  midi: number;
  velocity: number;
  spelling: ScoreSpelling | null;
  voice: number;
  staff: number;
  tie: { start: boolean; stop: boolean };
  confidence: number;
  provenance: NoteProvenance;
  provenance_run_id: string | null;
  flags: string[];
  status: NoteStatus;
  snap_candidates: { id: string; resolution: string; tick: number; score: number }[];
  selected_snap: string | null;
  ai_reason: string | null;
}

export interface ScorePart {
  id: string;
  name: string;
  midi_program: number;
  stem_source: string | null;
  staves: number;
  clefs: { staff: number; sign: string; line: number }[];
  notes: ScoreNote[];
  pedals: {
    start_sec: number;
    stop_sec: number;
    start_tick: number | null;
    stop_tick: number | null;
  }[];
}

export interface ScoreIR {
  schema_version: number;
  project_id: string;
  source: { filename: string; duration_sec: number; sample_rate: number };
  divisions: number;
  tempo_map: { bar: number; beat: number; bpm: number }[];
  time_signatures: { bar: number; numerator: number; denominator: number }[];
  key_signatures: { bar: number; fifths: number; mode: string }[];
  chords: { bar: number; beat: number; symbol: string; confidence: number }[];
  parts: ScorePart[];
  meta: { stages: Record<string, unknown> };
  next_note_id: number;
}

/**
 * #31-M3レビュー指摘: `ScoreOpsRequest`(リクエストボディ)は`ScoreIR`と異なり
 * コンポーネント名の衝突が無いため`generated.ts`に型が生成される。手書きで
 * 複製するとバックエンドのスキーマ変更(例: フィールド追加/null許容の変更)に
 * 黙って乖離しうるため、generated型から合成する。
 *
 * ただし`NoteAddOp`の`velocity`/`voice`/`staff`はPydantic側に`default`があり
 * リクエストでは省略可能だが、openapi-typescriptはdefault付きフィールドも
 * `required`として生成する(コード生成ツールの既知の制約)。実際の省略可能性に
 * 合わせ`Partial`で上書きする。
 */
type NoteAddOp = Omit<components["schemas"]["NoteAddOp"], "velocity" | "voice" | "staff"> &
  Partial<Pick<components["schemas"]["NoteAddOp"], "velocity" | "voice" | "staff">>;

export type NoteOp =
  | NoteAddOp
  | components["schemas"]["NoteUpdateOp"]
  | components["schemas"]["NoteDeleteOp"]
  | components["schemas"]["NoteSplitOp"]
  | components["schemas"]["NoteMergeOp"]
  | components["schemas"]["PartTransposeOctaveOp"];

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

/** #24: Stage 3 ピアノAMT(採譜)を実行する。 */
export async function runTranscribeStage(projectId: string): Promise<{ job_id: string }> {
  return runStage(projectId, "transcribe", {});
}

/** #25/#26: Stage 4 決定論的クオンタイズ + L0整音を実行する。 */
export async function runQuantizeStage(projectId: string): Promise<{ job_id: string }> {
  return runStage(projectId, "quantize", {});
}

/**
 * #27: Stage 6 MusicXML書き出し。生成物をブラウザのダウンロードとして保存させる
 * (`<a download>`方式。認証ヘッダ付きfetchが必要なため`<a href>`直リンクは使えない)。
 * バックエンドはMIDIも生成できるが(`format: "midi"`)、M2のフロントエンド最小
 * スコープ(ユーザー決定済み)ではMusicXMLのみを露出する。到達しないコード
 * (未使用のformatパラメータ)を残さないため、あえて汎用化しない。
 */
export async function exportMusicXml(projectId: string): Promise<void> {
  const resp = await apiFetch(`/api/projects/${projectId}/export`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ format: "musicxml" }),
  });
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "score.musicxml";
  // ダウンロード開始前にURLが無効化されるのを避けるため、`document.body`への
  // 追加とclick()の完了を1マクロタスク挟んでから`revokeObjectURL`する
  // (#27-M2レビュー指摘: `a.click()`直後の同期的なrevokeは、一部環境で
  // ダウンロード自体の失敗を招きうる)。
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/** #23: Score IR全体。採譜(transcribe)未実行なら`null`(404を例外にしない、getBeatmapと同じ方針)。 */
export async function getScore(projectId: string): Promise<ScoreIR | null> {
  const resp = await apiFetch(`/api/projects/${projectId}/score`, { allowNotFound: true });
  if (!resp) return null;
  return (await resp.json()) as ScoreIR;
}

/**
 * #31: ノート編集オペレーションを配列で一括適用する。成功時は更新後のScore IR
 * (サーバの権威ある状態)を返す。404(score/beatmap未実行)・409(並行更新)・
 * 422(不正なop)は`apiFetch`が例外として送出する。
 */
export async function applyScoreOps(projectId: string, ops: NoteOp[]): Promise<ScoreIR> {
  const resp = await apiFetch(`/api/projects/${projectId}/score/ops`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ops }),
  });
  return (await resp.json()) as ScoreIR;
}

/**
 * #32: `/score/undo`・`/score/redo`の戻り値。`GET /score`/`applyScoreOps`と
 * 同じ理由(コンポーネント名衝突回避)でgenerated型が無く、手書きする。
 * `applied=false`はUndo/Redoスタックが空で何も変化しなかったことを示す
 * (200のまま返る、「元に戻す操作が無い」はエラーではないため)。
 */
export interface UndoRedoResult {
  score: ScoreIR;
  applied: boolean;
}

/** #32: 直近の編集を1つ元に戻す。404(score未実行)・409(並行更新)は`apiFetch`が例外として送出する。 */
export async function undoScoreOps(projectId: string): Promise<UndoRedoResult> {
  const resp = await apiFetch(`/api/projects/${projectId}/score/undo`, { method: "POST" });
  return (await resp.json()) as UndoRedoResult;
}

/** #32: 直近にUndoした編集を1つやり直す。404/409は`apiFetch`が例外として送出する。 */
export async function redoScoreOps(projectId: string): Promise<UndoRedoResult> {
  const resp = await apiFetch(`/api/projects/${projectId}/score/redo`, { method: "POST" });
  return (await resp.json()) as UndoRedoResult;
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
