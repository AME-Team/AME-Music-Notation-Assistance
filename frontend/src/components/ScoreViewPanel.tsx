import { runQuantizeStage } from "../api/client";
import { scoreKey, useScore } from "../hooks/useScore";
import { useStageRunner } from "../hooks/useStageRunner";
import { PREVIEW_BARS_CHOICES, previewNotes, previewWindowResult } from "../lib/scoreView";
import type { StepId } from "../lib/workflow";
import { useScoreViewStore } from "../stores/scoreViewStore";
import { MidiBar } from "./MidiBar";
import { ScorePreview } from "./ScorePreview";

interface ScoreViewPanelProps {
  projectId: string;
  /** いま開いている作業ステップ(下流の作業を壊さない判断に使う)。 */
  activeStep: StepId;
}

/**
 * ④を再実行してよいステップ。⑤リファイン以降は手動補正の結果を持っているため、
 * ここから④を再実行すると下流の作業を上書き・無効化しうる(MIDDLEレビュー指摘)。
 */
const QUANTIZE_RERUN_STEPS: readonly StepId[] = ["separate", "beat", "transcribe", "quantize"];

/**
 * そのページが自前で楽譜プレビューを出すステップ(⑤⑥は`DiffPanel`等が
 * `ScorePreview`を持つ)。同じ画面でOSMDを二重に走らせないため、パネル側の
 * 埋め込み楽譜は出さない(MIDDLEレビュー指摘)。
 */
const STEPS_WITH_OWN_SCORE: readonly StepId[] = ["refine", "review"];

/** パネルはノート選択を持たないため、再レンダーで作り直さないよう固定する。 */
const NO_SELECTION: ReadonlySet<number> = new Set();

/** 表示できない理由ごとの案内文(1つに丸めると誤誘導になる)。 */
const UNAVAILABLE_MESSAGE = {
  "no-notes": "④クオンタイズが完了すると表示できます(音符にtickがまだ付いていません)。",
  "invalid-divisions": "スコアの分解能(divisions)が不正なため、小節単位で表示できません。",
  "too-short": "曲が1小節に満たないため、小節単位のプレビューを作れません。",
} as const;

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
export function ScoreViewPanel({ projectId, activeStep }: ScoreViewPanelProps) {
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

  // 範囲か理由かを1回の解析で受け取る(同じ解析を2回走らせない)。
  const analysis = score ? previewWindowResult(score, bars) : null;
  const barWindow = analysis && "window" in analysis ? analysis.window : null;
  const unavailableReason = analysis && "reason" in analysis ? analysis.reason : null;
  const canRerunQuantize = QUANTIZE_RERUN_STEPS.includes(activeStep);
  const showsOwnScore = STEPS_WITH_OWN_SCORE.includes(activeStep);
  const notes = score && barWindow ? previewNotes(score, barWindow) : [];

  return (
    <section
      className="space-y-2 rounded-lg border border-gray-200 p-4 dark:border-gray-700"
      data-testid="score-view-panel"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <h3 className="text-lg font-semibold text-gray-700 dark:text-gray-200">
          先頭{barWindow ? barWindow.toBar : bars}小節のプレビュー(
          {showsOwnScore ? "MIDI" : "MIDI・楽譜"})
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
          disabled={runner.isRunning || !score || !canRerunQuantize}
          className="rounded-md bg-gray-800 px-3 py-1.5 text-sm font-medium text-white hover:bg-gray-900 disabled:opacity-50"
        >
          {runner.isRunning ? "更新中..." : "ビート補正を反映して更新"}
        </button>
        <span className="text-xs text-gray-500 dark:text-gray-400">
          {canRerunQuantize
            ? `ビート・拍子・テンポを補正したら、ここで先頭${barWindow ? barWindow.toBar : bars}小節を作り直せます`
            : "このページの作業(⑤以降)を上書きしないよう、ここからは④を再実行できません"}
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

      {score && unavailableReason && (
        <p className="text-sm text-gray-500 dark:text-gray-400">
          {UNAVAILABLE_MESSAGE[unavailableReason]}
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
          {showsOwnScore ? (
            <p className="text-xs text-gray-500 dark:text-gray-400">
              このページは元から譜面を表示しているため、パネルはMIDIバーのみ表示します。
            </p>
          ) : (
            <ScorePreview
              projectId={projectId}
              score={score}
              selectedNoteIds={NO_SELECTION}
              fromBar={1}
              toBar={barWindow.toBar}
              showHeading={false}
            />
          )}
        </div>
      )}
    </section>
  );
}
