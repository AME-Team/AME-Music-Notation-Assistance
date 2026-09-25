import { runQuantizeStage } from "../api/client";
import { scoreKey, useScore } from "../hooks/useScore";
import { useStageRunner } from "../hooks/useStageRunner";
import { PREVIEW_BARS_CHOICES, previewNotes, previewWindow } from "../lib/scoreView";
import { useScoreViewStore } from "../stores/scoreViewStore";
import { MidiBar } from "./MidiBar";
import { ScorePreview } from "./ScorePreview";

interface ScoreViewPanelProps {
  projectId: string;
}

/** パネルはノート選択を持たないため、再レンダーで作り直さないよう固定する。 */
const NO_SELECTION: ReadonlySet<number> = new Set();

/**
 * #172: **全ての作業ページ**に常設する「先頭N小節プレビュー」。
 *
 * 補正は実際のMIDIと楽譜を見ながら行う必要がある(このアプリの根幹)。以前は
 * 補正できる②には楽譜もMIDIも無く、楽譜が出る⑤⑥には補正手段が無かったため、
 * 数値を勘で入れるしかなかった。ここでは先頭N小節(Nは設定・既定4)を常に表示し、
 * ビートや拍子を補正したら「④を再実行して更新」で作り直せるようにする。
 *
 * 表示は読み取り専用の`MidiBar`と、既存の`ScorePreview`(表示範囲だけ固定)を使う。
 */
export function ScoreViewPanel({ projectId }: ScoreViewPanelProps) {
  const bars = useScoreViewStore((s) => s.bars);
  const setBars = useScoreViewStore((s) => s.setBars);
  const scoreQuery = useScore(projectId);
  const score = scoreQuery.data ?? null;

  const runner = useStageRunner(() => runQuantizeStage(projectId), {
    invalidateKeys: [[...scoreKey(projectId)]],
    failureFallbackMessage: "先頭N小節の再作成に失敗しました",
    stage: "quantize",
    label: "④ クオンタイズ(プレビューの更新)",
  });

  const barWindow = score ? previewWindow(score, bars) : null;
  const notes = score && barWindow ? previewNotes(score, barWindow) : [];

  return (
    <section
      className="space-y-2 rounded-lg border border-gray-200 p-4 dark:border-gray-700"
      data-testid="score-view-panel"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <h3 className="text-lg font-semibold text-gray-700 dark:text-gray-200">
          先頭{barWindow ? barWindow.toBar : bars}小節のプレビュー(MIDI・楽譜)
        </h3>
        <label className="flex items-center gap-1 text-sm text-gray-600 dark:text-gray-300">
          表示する小節数
          <select
            value={bars}
            onChange={(event) => setBars(Number(event.target.value))}
            className="rounded-md border border-gray-300 px-2 py-1 text-sm dark:border-gray-600"
            aria-label="表示する小節数"
          >
            {PREVIEW_BARS_CHOICES.map((value) => (
              <option key={value} value={value}>
                {value} 小節
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          onClick={() => void runner.run()}
          disabled={runner.isRunning || !score}
          className="rounded-md bg-gray-800 px-3 py-1.5 text-sm font-medium text-white hover:bg-gray-900 disabled:opacity-50"
        >
          {runner.isRunning ? "更新中..." : "ビート補正を反映して更新"}
        </button>
        <span className="text-xs text-gray-500 dark:text-gray-400">
          ビート・拍子・テンポを補正したら、ここで先頭{barWindow ? barWindow.toBar : bars}
          小節を作り直せます
        </span>
      </div>

      {runner.error && <p className="text-sm text-red-600 dark:text-red-400">{runner.error}</p>}

      {scoreQuery.isPending && (
        <p className="text-sm text-gray-500 dark:text-gray-400">読み込み中...</p>
      )}

      {!scoreQuery.isPending && !score && (
        <p className="text-sm text-gray-500 dark:text-gray-400">
          ③採譜が完了すると、ここに先頭{barWindow ? barWindow.toBar : bars}
          小節のMIDIと楽譜が出ます。
        </p>
      )}

      {score && !barWindow && (
        <p className="text-sm text-gray-500 dark:text-gray-400">
          ④クオンタイズが完了すると表示できます(音符に音符位置(tick)がまだ付いていません)。
        </p>
      )}

      {score && barWindow && (
        <div className="space-y-2">
          <MidiBar notes={notes} window={barWindow} />
          {notes.length === 0 && (
            <p className="text-sm text-gray-500 dark:text-gray-400">
              先頭{barWindow.toBar}小節に音符がありません。
            </p>
          )}
          <ScorePreview
            projectId={projectId}
            score={score}
            selectedNoteIds={NO_SELECTION}
            fromBar={1}
            toBar={barWindow.toBar}
          />
        </div>
      )}
    </section>
  );
}
