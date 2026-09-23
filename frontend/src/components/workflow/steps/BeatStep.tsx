import { type ReactNode, useState } from "react";
import type { Beatmap, Project } from "../../../api/client";
import type { useStageRunner } from "../../../hooks/useStageRunner";
import {
  formatBeatPosition,
  formatBpm,
  formatSeconds,
  formatTempoNote,
  formatTimeSignatureChanges,
  MAX_VISIBLE_ROWS,
  summarizeBeatmap,
} from "../../../lib/beatSummary";
import { BeatGridEditor } from "../../BeatGridEditor";
import { BeatGridOverlay } from "../../BeatGridOverlay";
import { Term } from "../../ui/Term";
import { Waveform } from "../../Waveform";
import { StepShell } from "../StepShell";

interface BeatStepProps {
  projectId: string;
  project: Project | undefined;
  peaks: { peaks: number[][]; duration_sec: number } | undefined;
  peaksLoading: boolean;
  peaksError: Error | null;
  beatmap: Beatmap | null | undefined;
  beatmapError: Error | null;
  runner: ReturnType<typeof useStageRunner>;
  onBack: () => void;
  onNext: () => void;
}

/** 検出結果パネルの1項目(見出し・値・補足)。 */
function ResultItem({ label, value, note }: { label: ReactNode; value: string; note?: string }) {
  return (
    <div className="rounded-md bg-gray-50 dark:bg-gray-800/60 px-3 py-2">
      <dt className="text-xs text-gray-500 dark:text-gray-400">{label}</dt>
      <dd className="mt-0.5">
        <span className="text-base font-semibold text-gray-900 dark:text-gray-100">{value}</span>
        {note && <span className="ml-1 text-xs text-gray-500 dark:text-gray-400">{note}</span>}
      </dd>
    </div>
  );
}

/**
 * ②テンポ・拍の検出: 以降のすべての工程が拍の位置を基準にするため、最初に必要。
 *
 * #167: 以前は波形(ビート線入り)と`BeatGridEditor`(補正フォーム)しか無く、
 * **推定値そのもの**が画面から読み取れなかった。ここでは`beatmap.json`を
 * `lib/beatSummary.ts`で集計し、テンポ(BPM)・拍子・拍の規模と拍一覧を表示する。
 */
export function BeatStep({
  projectId,
  project,
  peaks,
  peaksLoading,
  peaksError,
  beatmap,
  beatmapError,
  runner,
  onBack,
  onNext,
}: BeatStepProps) {
  const stage = project?.stages.beat;
  const isDone = stage?.status === "succeeded" && !stage.stale;
  // 拍一覧は初期は先頭のみ表示し、長い曲では全件表示に切り替えられるようにする。
  const [showAllRows, setShowAllRows] = useState(false);

  const summary = beatmap ? summarizeBeatmap(beatmap) : null;
  const timeSignatureNote = summary ? formatTimeSignatureChanges(summary) : null;
  const visibleRows = summary
    ? showAllRows
      ? summary.rows
      : summary.rows.slice(0, MAX_VISIBLE_ROWS)
    : [];

  return (
    <StepShell
      stepId="beat"
      stepIndex={2}
      onBack={onBack}
      onNext={isDone ? onNext : undefined}
      nextDisabledReason={isDone ? undefined : "テンポ・拍の検出を完了すると次に進めます"}
    >
      <button
        type="button"
        onClick={() => void runner.run()}
        disabled={runner.isRunning}
        className="rounded-md bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 disabled:opacity-50"
      >
        {runner.isRunning ? "検出を実行中..." : isDone ? "やり直す" : "検出を開始する"}
      </button>
      {runner.error && <p className="text-sm text-red-600 dark:text-red-400">{runner.error}</p>}

      {summary && (
        <section className="space-y-4 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
          <div className="flex items-center justify-between">
            <h3 className="text-lg font-semibold text-gray-700 dark:text-gray-200">検出結果</h3>
            <span className="rounded-md bg-gray-100 dark:bg-gray-800 px-2 py-1 text-xs text-gray-600 dark:text-gray-300">
              信頼度 {summary.confidencePercent}%
            </span>
          </div>

          {summary.beatCount === 0 ? (
            <p className="text-sm text-amber-600 dark:text-amber-400">
              拍を検出できませんでした(音源が短すぎる、または打点がはっきりしない曲の可能性があります)。
            </p>
          ) : (
            <>
              <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                <ResultItem
                  label={<Term k="bpm">テンポ</Term>}
                  value={`${formatBpm(summary.tempoBpm)} BPM`}
                  note={formatTempoNote(summary)}
                />
                <ResultItem
                  label={<Term k="timeSignature">拍子</Term>}
                  value={summary.timeSignature ?? "—"}
                  note={timeSignatureNote ?? undefined}
                />
                <ResultItem
                  label={<Term k="beatUnit">拍の数</Term>}
                  value={`${summary.beatCount} 拍`}
                  note={`小節 ${summary.barCount}・ダウンビート ${summary.downbeatCount} 箇所`}
                />
                <ResultItem
                  label="平均拍間隔"
                  value={formatSeconds(summary.beatIntervalSec)}
                  note={`先頭 ${formatSeconds(summary.firstBeatSec)} / 最後 ${formatSeconds(summary.lastBeatSec)}`}
                />
                {summary.pickupBeatCount > 0 && (
                  <ResultItem
                    label={<Term k="pickupBeat">ピックアップ拍</Term>}
                    value={`${summary.pickupBeatCount} 拍`}
                    note="最初のダウンビートより前(小節の外)"
                  />
                )}
              </dl>

              <div className="space-y-2">
                <h4 className="text-sm font-medium text-gray-700 dark:text-gray-200">拍の一覧</h4>
                <div className="max-h-80 overflow-auto rounded-md border border-gray-200 dark:border-gray-700">
                  <table className="min-w-full text-sm">
                    <thead className="sticky top-0 bg-gray-50 dark:bg-gray-800">
                      <tr>
                        <th
                          scope="col"
                          className="px-3 py-1.5 text-left font-medium text-gray-500 dark:text-gray-400"
                        >
                          拍
                        </th>
                        <th
                          scope="col"
                          className="px-3 py-1.5 text-left font-medium text-gray-500 dark:text-gray-400"
                        >
                          位置(<Term k="bar">小節</Term>.拍)
                        </th>
                        <th
                          scope="col"
                          className="px-3 py-1.5 text-left font-medium text-gray-500 dark:text-gray-400"
                        >
                          時刻
                        </th>
                        <th
                          scope="col"
                          className="px-3 py-1.5 text-left font-medium text-gray-500 dark:text-gray-400"
                        >
                          BPM(直前の拍から)
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {visibleRows.map((row) => (
                        <tr
                          key={row.index}
                          className="border-t border-gray-100 dark:border-gray-800"
                        >
                          <td className="px-3 py-1 text-gray-500 dark:text-gray-400">
                            {row.index}
                          </td>
                          <td className="px-3 py-1 text-gray-900 dark:text-gray-100">
                            {formatBeatPosition(row)}
                          </td>
                          <td className="px-3 py-1 text-gray-700 dark:text-gray-300">
                            {formatSeconds(row.timeSec)}
                          </td>
                          <td className="px-3 py-1 text-gray-700 dark:text-gray-300">
                            {formatBpm(row.bpm)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {summary.rows.length > MAX_VISIBLE_ROWS && (
                  <button
                    type="button"
                    onClick={() => setShowAllRows((value) => !value)}
                    className="text-xs text-blue-600 dark:text-blue-400 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
                  >
                    {showAllRows
                      ? `先頭 ${MAX_VISIBLE_ROWS} 拍のみ表示`
                      : `すべて表示(${summary.rows.length} 拍)`}
                  </button>
                )}
              </div>
            </>
          )}
        </section>
      )}

      {peaksLoading && (
        <p className="text-sm text-gray-500 dark:text-gray-400">波形を読み込み中...</p>
      )}
      {peaksError && <p className="text-sm text-red-600 dark:text-red-400">{peaksError.message}</p>}
      {beatmapError && (
        <p className="text-sm text-red-600 dark:text-red-400">{beatmapError.message}</p>
      )}
      {peaks && (
        <div className="relative">
          <Waveform peaks={peaks.peaks} />
          {beatmap && <BeatGridOverlay beatmap={beatmap} durationSec={peaks.duration_sec} />}
        </div>
      )}
      {beatmap && <BeatGridEditor projectId={projectId} beatmap={beatmap} />}
    </StepShell>
  );
}
