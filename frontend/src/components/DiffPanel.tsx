import { useMemo } from "react";
import type { NoteChangeResponse } from "../api/client";
import { useAgentDiff } from "../hooks/useAgentDiff";
import { useAgentReport } from "../hooks/useAgentReport";
import { useDiff } from "../hooks/useDiff";

interface DiffPanelProps {
  projectId: string;
  runId: string;
  onDismiss: () => void;
  /**
   * #51: L1整音(`/api/projects/{id}/refine/runs/{run_id}/diff`、既定)か
   * L2エージェントrun(`/api/agent/runs/{run_id}/diff`、#46)かで差分の
   * 取得元エンドポイントが異なる。承認/却下UIとreport.md表示は共通。
   */
  source?: "refine" | "agent";
}

interface SpellingLike {
  step: string;
  alter: number | null;
  octave: number;
}

function isSpellingLike(value: unknown): value is SpellingLike {
  return typeof value === "object" && value !== null && "step" in value && "octave" in value;
}

function formatSpelling(value: unknown): string {
  if (!isSpellingLike(value)) return "?";
  const accidental =
    value.alter === 2
      ? "##"
      : value.alter === 1
        ? "#"
        : value.alter === -1
          ? "b"
          : value.alter === -2
            ? "bb"
            : "";
  return `${value.step}${accidental}${value.octave}`;
}

const CHANGED_FIELD_LABEL: Record<string, (change: NoteChangeResponse) => string> = {
  spelling: (c) =>
    `異名同音: ${formatSpelling(c.before.spelling)} → ${formatSpelling(c.after.spelling)}`,
  onset_tick: () => "拍位置のスナップ移動",
  voice: (c) => `声部: ${String(c.before.voice)} → ${String(c.after.voice)}`,
  staff: (c) => `譜表: ${String(c.before.staff)} → ${String(c.after.staff)}`,
  tie: () => "タイの変更",
};

/** 「第12小節: ノートC4の異名同音修正 B#3 → C4」のような1行説明を組み立てる(FR-10)。 */
function describeChange(change: NoteChangeResponse): string {
  if (change.change_type === "delete") {
    return `削除提案 (MIDI ${change.midi})`;
  }
  if (change.change_type === "split_tie") {
    return `音符分割 (タイ、MIDI ${change.midi})`;
  }
  const labels = change.changed_fields.map(
    (field) => CHANGED_FIELD_LABEL[field]?.(change) ?? field,
  );
  return labels.length > 0 ? labels.join(" / ") : "変更";
}

const CHANGE_TYPE_BADGE: Record<NoteChangeResponse["change_type"], string> = {
  keep: "bg-indigo-50 text-indigo-700 border-indigo-200",
  delete: "bg-red-50 text-red-700 border-red-200",
  split_tie: "bg-amber-50 text-amber-700 border-amber-200",
};

/**
 * #41: DiffPanel — L0とAI提案(L1)の差分を小節単位で提示し、承認/却下を行う(FR-10)。
 *
 * `RefineSection`が返す`run_id`をそのまま受け取る。バックエンドが
 * `score/staging/{run_id}.json`とcurrentを突き合わせて計算した差分のみを表示し
 * (`api/diff.py`)、クライアント側での差分計算は行わない。
 *
 * ノートの出自による色分け表示(FR-10)自体はPianoRoll側(#35)が担当する。承認/却下後は
 * `useDiff`が`useScore`のキャッシュを更新するため、ScorePreview/PianoRollは
 * 自動的に再描画される(このコンポーネント自身が明示的に再読み込みを指示する必要はない)。
 */
export function DiffPanel({ projectId, runId, onDismiss, source = "refine" }: DiffPanelProps) {
  const refineDiff = useDiff(projectId, source === "refine" ? runId : null);
  const agentDiff = useAgentDiff(projectId, source === "agent" ? runId : null);
  const { diff, isLoading, error, accept, reject } = source === "agent" ? agentDiff : refineDiff;
  const { report } = useAgentReport(runId);

  const changesByBar = useMemo(() => {
    const groups = new Map<number, NoteChangeResponse[]>();
    for (const change of diff?.changes ?? []) {
      const bucket = groups.get(change.bar) ?? [];
      bucket.push(change);
      groups.set(change.bar, bucket);
    }
    return [...groups.entries()].sort(([a], [b]) => a - b);
  }, [diff]);

  const isBusy = accept.isPending || reject.isPending;
  const mutationError = (accept.error ?? reject.error) as Error | null;

  return (
    <section className="space-y-4 rounded-lg border border-gray-200 p-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-semibold text-gray-700">DiffPanel — L0 / AI提案の差分</h3>
          <p className="text-xs text-gray-500">
            小節単位でAIの変更提案を確認し、承認または却下してください (設計書§4.1 FR-10)
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span className="rounded bg-indigo-50 px-2 py-0.5 text-xs font-mono text-indigo-700 border border-indigo-200">
            Run ID: {runId}
          </span>
          <button
            type="button"
            onClick={onDismiss}
            className="rounded-md px-2 py-1 text-xs text-gray-500 hover:bg-gray-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-gray-400"
          >
            閉じる
          </button>
        </div>
      </div>

      {report && (
        <details className="rounded-md border border-indigo-100 bg-indigo-50/50 p-3 text-sm" open>
          <summary className="cursor-pointer font-medium text-indigo-900 select-none">
            AI成果報告 (report.md)
          </summary>
          <div className="mt-2 max-h-60 overflow-y-auto whitespace-pre-wrap rounded border border-indigo-100 bg-white p-3 font-mono text-xs text-gray-800">
            {report.content}
          </div>
        </details>
      )}

      {isLoading && <p className="text-sm text-gray-500">差分を読み込み中...</p>}
      {error && <p className="text-sm text-red-600">{error.message}</p>}

      {diff && changesByBar.length === 0 && (
        <p className="rounded-md bg-emerald-50 p-3 text-sm text-emerald-800 border border-emerald-200">
          処理待ちの変更はありません(すべて承認/却下済みです)。
        </p>
      )}

      {diff && changesByBar.length > 0 && (
        <>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => accept.mutate({})}
              disabled={isBusy}
              className="rounded-md bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-emerald-500 disabled:opacity-50"
            >
              すべて承認
            </button>
            <button
              type="button"
              onClick={() => reject.mutate({})}
              disabled={isBusy}
              className="rounded-md bg-gray-100 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-gray-400 disabled:opacity-50"
            >
              すべて却下
            </button>
          </div>

          {mutationError && <p className="text-sm text-red-600">{mutationError.message}</p>}

          <div className="space-y-4">
            {changesByBar.map(([bar, changes]) => (
              <div key={bar} className="space-y-2 rounded-md border border-gray-200 p-3">
                <div className="flex items-center justify-between">
                  <h4 className="text-sm font-semibold text-gray-700">第{bar}小節</h4>
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => accept.mutate({ bar_range: [bar, bar] })}
                      disabled={isBusy}
                      className="rounded-md bg-emerald-50 px-2 py-1 text-xs font-medium text-emerald-700 border border-emerald-200 hover:bg-emerald-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-emerald-500 disabled:opacity-50"
                    >
                      承認
                    </button>
                    <button
                      type="button"
                      onClick={() => reject.mutate({ bar_range: [bar, bar] })}
                      disabled={isBusy}
                      className="rounded-md bg-gray-50 px-2 py-1 text-xs font-medium text-gray-600 border border-gray-200 hover:bg-gray-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-gray-400 disabled:opacity-50"
                    >
                      却下
                    </button>
                  </div>
                </div>

                <ul className="space-y-2">
                  {changes.map((change) => (
                    <li key={change.note_ids.join("-")} className="flex flex-col gap-1 text-sm">
                      <div className="flex items-center gap-2">
                        <span
                          className={`rounded border px-1.5 py-0.5 text-xs font-medium ${CHANGE_TYPE_BADGE[change.change_type]}`}
                        >
                          {change.change_type}
                        </span>
                        <span className="text-gray-700">{describeChange(change)}</span>
                      </div>
                      {change.ai_reason && (
                        <p className="ml-1 rounded-md bg-gray-50 px-2 py-1 text-xs text-gray-500 border border-gray-100">
                          {change.ai_reason}
                        </p>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
