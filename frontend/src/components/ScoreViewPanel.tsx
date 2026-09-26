import { runQuantizeStage } from "../api/client";
import { scoreKey, useScore } from "../hooks/useScore";
import { useStageRunner } from "../hooks/useStageRunner";
import {
  appliedQuantizeSettings,
  quantizeStatusLine,
  toStageParams,
} from "../lib/quantizeSettings";
import {
  normalizeScoreLayout,
  PREVIEW_BARS_CHOICES,
  previewNotes,
  previewWindowResult,
  STEPS_WITH_OWN_SCORE,
  ZOOM_MAX,
  ZOOM_MIN,
  ZOOM_STEP,
} from "../lib/scoreView";
import type { StepId } from "../lib/workflow";
import { useQuantizeStore } from "../stores/quantizeStore";
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

/** パネルはノート選択を持たないため、再レンダーで作り直さないよう固定する。 */
const NO_SELECTION: ReadonlySet<number> = new Set();

/** 表示できない理由ごとの案内文(1つに丸めると誤誘導になる)。 */
const UNAVAILABLE_MESSAGE = {
  "no-notes": "④クオンタイズが完了すると表示できます(音符にtickがまだ付いていません)。",
  "invalid-divisions": "スコアの分解能(divisions)が不正なため、小節単位で表示できません。",
  "too-short": "曲が1小節に満たないため、小節単位のプレビューを作れません。",
} as const;

/**
 * #179: **全ての作業ページの最上部**に固定する「楽譜とMIDI」ビュー。
 *
 * 楽譜清書ソフトでは5線譜がメインであり、補正は実際の楽譜とMIDIを見ながら行う
 * (このアプリの根幹)。#172では補正UIの**下**に置いていたため、補正中に楽譜が
 * 画面外へスクロールし「楽譜を見ながら補正」ができなかった。
 *
 * ここでは先頭N小節(Nは設定・既定4)のMIDIバーと5線譜を最上部に固定し、5線譜は
 * 1本の横方向の段に並べる(OSMDの`renderSingleHorizontalStaffline`)。ビートや拍子を
 * 補正したら「ビート補正を反映して更新」で作り直せる。
 */
export function ScoreViewPanel({ projectId, activeStep }: ScoreViewPanelProps) {
  const bars = useScoreViewStore((s) => s.bars);
  const setBars = useScoreViewStore((s) => s.setBars);
  const zoom = useScoreViewStore((s) => s.zoom);
  const setZoom = useScoreViewStore((s) => s.setZoom);
  const layout = useScoreViewStore((s) => s.layout);
  const setLayout = useScoreViewStore((s) => s.setLayout);
  const scoreQuery = useScore(projectId);
  const score = scoreQuery.data ?? null;

  // 実行の直前に保存値を取り出す(古いクロージャの設定を送らないため)。
  const runner = useStageRunner(
    () => runQuantizeStage(projectId, toStageParams(useQuantizeStore.getState().settings)),
    {
      invalidateKeys: [[...scoreKey(projectId)]],
      failureFallbackMessage: "先頭N小節の再作成に失敗しました",
      stage: "quantize",
      label: "④ クオンタイズ(プレビューの更新)",
    },
  );
  const quantizeSettings = useQuantizeStore((s) => s.settings);
  // 「適用中」は表示中の楽譜を作った設定(スコアの meta.stages)であって、
  // 保存値(次回の実行で使う設定)ではない。
  const appliedQuantize = score ? appliedQuantizeSettings(score) : null;

  // 範囲か理由かを1回の解析で受け取る(同じ解析を2回走らせない)。
  const analysis = score ? previewWindowResult(score, bars) : null;
  const barWindow = analysis && "window" in analysis ? analysis.window : null;
  const unavailableReason = analysis && "reason" in analysis ? analysis.reason : null;
  const canRerunQuantize = QUANTIZE_RERUN_STEPS.includes(activeStep);
  const showsOwnScore = STEPS_WITH_OWN_SCORE.includes(activeStep);
  const notes = score && barWindow ? previewNotes(score, barWindow) : [];
  const shownBars = barWindow ? barWindow.toBar : bars;

  return (
    <section
      // 作業ステップの操作をスクロールしても楽譜とMIDIが隠れないよう、最上部に固定する。
      // 負マージンで親の外へはみ出させない(親に左右パディングが無いため、横スクロールの
      // 原因になる:LOWレビュー指摘)。通常のパディングで固定する。
      className="sticky top-0 z-10 space-y-2 border-b border-gray-200 bg-white/95 py-3 backdrop-blur dark:border-gray-700 dark:bg-gray-950/95"
      data-testid="score-view-panel"
      aria-label="楽譜とMIDIのプレビュー"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <h2 className="text-base font-semibold text-gray-800 dark:text-gray-100">
          {/* 見出しは実際に描いているものに合わせる(⑤⑥は自前の譜面があるためMIDIのみ)。 */}
          {showsOwnScore ? `MIDI(先頭${shownBars}小節)` : `楽譜とMIDI(先頭${shownBars}小節)`}
        </h2>
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
        <label className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-300">
          縮尺
          <input
            type="range"
            min={Math.round(ZOOM_MIN * 100)}
            max={Math.round(ZOOM_MAX * 100)}
            step={Math.round(ZOOM_STEP * 100)}
            value={Math.round(zoom * 100)}
            onChange={(event) => setZoom(Number(event.target.value) / 100)}
            className="w-28"
            aria-label="楽譜の縮尺"
          />
          <span
            className="w-11 text-right text-xs text-gray-500 dark:text-gray-400"
            data-testid="score-zoom"
          >
            {Math.round(zoom * 100)}%
          </span>
        </label>
        <label className="flex items-center gap-1 text-sm text-gray-600 dark:text-gray-300">
          並べ方
          <select
            value={layout}
            onChange={(event) => setLayout(normalizeScoreLayout(event.target.value))}
            className="rounded-md border border-gray-300 px-2 py-1 text-sm dark:border-gray-600"
            aria-label="5線譜の並べ方"
          >
            <option value="single-line">横に1段</option>
            <option value="wrap">幅に合わせる</option>
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
        <span className="text-xs text-gray-500 dark:text-gray-400" data-testid="quantize-summary">
          {quantizeStatusLine(appliedQuantize, quantizeSettings)}
        </span>
      </div>

      <p className="text-xs text-gray-500 dark:text-gray-400">
        {canRerunQuantize
          ? `ビート・拍子・テンポを補正したら、ここで先頭${shownBars}小節を作り直せます`
          : "このページの作業(⑤以降)を上書きしないよう、ここからは④を再実行できません"}
      </p>

      {runner.error && <p className="text-sm text-red-600 dark:text-red-400">{runner.error}</p>}

      {scoreQuery.isPending && (
        <p className="text-sm text-gray-500 dark:text-gray-400">読み込み中...</p>
      )}

      {!scoreQuery.isPending && !score && (
        <p className="text-sm text-gray-500 dark:text-gray-400">
          ③採譜が完了すると、ここに先頭{shownBars}小節のMIDIと楽譜が出ます。
        </p>
      )}

      {score && unavailableReason && (
        <p className="text-sm text-gray-500 dark:text-gray-400">
          {UNAVAILABLE_MESSAGE[unavailableReason]}
        </p>
      )}

      {score && barWindow && (
        <div className="space-y-1">
          {/* MIDIバーと5線譜を並べて置き、同じ区間を同時に見ながら補正する。
              高さは上限付き(stickyで画面を占めるため、無制限だと楽譜より下の
              セクションが常に隠れてしまう)。 */}
          <div
            className="max-h-[38vh] overflow-y-auto rounded-md border border-gray-200 bg-white dark:border-gray-700"
            data-testid="score-main-scroll"
          >
            <div className="space-y-1 p-1">
              <MidiBar notes={notes} window={barWindow} />
              {notes.length === 0 && (
                <p className="text-sm text-gray-500 dark:text-gray-400">
                  先頭{shownBars}小節に音符がありません。
                </p>
              )}
              {showsOwnScore ? (
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  このページは自前の譜面(差分表示)が下にあるため、ここはMIDIバーのみ表示します。
                </p>
              ) : (
                <ScorePreview
                  // 並べ方を切り替えたらOSMDインスタンスを作り直す(横並びは生成時
                  // オプションのため。`ScorePreview`の`singleHorizontalStaffline`参照)。
                  key={layout}
                  projectId={projectId}
                  score={score}
                  selectedNoteIds={NO_SELECTION}
                  fromBar={1}
                  toBar={barWindow.toBar}
                  showHeading={false}
                  singleHorizontalStaffline={layout === "single-line"}
                  zoom={zoom}
                />
              )}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
