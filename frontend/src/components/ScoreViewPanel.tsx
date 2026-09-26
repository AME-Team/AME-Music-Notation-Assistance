import { runQuantizeStage } from "../api/client";
import { scoreKey, useScore } from "../hooks/useScore";
import { useStageRunner } from "../hooks/useStageRunner";
import {
  appliedQuantizeSettings,
  quantizeStatusLine,
  toStageParams,
} from "../lib/quantizeSettings";
import {
  PREVIEW_BARS_CHOICES,
  previewNotes,
  previewWindowResult,
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

const ZOOM_BUTTON_CLASS =
  "rounded-md border border-gray-300 dark:border-gray-600 px-2 py-0.5 text-sm text-gray-700 " +
  "dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-40";

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
  const notes = score && barWindow ? previewNotes(score, barWindow) : [];
  const shownBars = barWindow ? barWindow.toBar : bars;

  return (
    <section
      // 作業ステップの操作をスクロールしても楽譜とMIDIが隠れないよう、最上部に固定する。
      className="-mx-4 sticky top-0 z-10 space-y-2 border-b border-gray-200 bg-white/95 px-4 py-3 backdrop-blur dark:border-gray-700 dark:bg-gray-950/95"
      data-testid="score-view-panel"
      aria-label="楽譜とMIDIのプレビュー"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <h2 className="text-base font-semibold text-gray-800 dark:text-gray-100">
          楽譜とMIDI(先頭{shownBars}小節)
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
        {/* ズームの増減(ボタン自体にaria-labelを付ける)。 */}
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => setZoom(zoom - ZOOM_STEP)}
            disabled={zoom <= ZOOM_MIN}
            className={ZOOM_BUTTON_CLASS}
            aria-label="楽譜を縮小"
          >
            −
          </button>
          <span
            className="w-12 text-center text-xs text-gray-500 dark:text-gray-400"
            data-testid="score-zoom"
          >
            {Math.round(zoom * 100)}%
          </span>
          <button
            type="button"
            onClick={() => setZoom(zoom + ZOOM_STEP)}
            disabled={zoom >= ZOOM_MAX}
            className={ZOOM_BUTTON_CLASS}
            aria-label="楽譜を拡大"
          >
            ＋
          </button>
        </div>
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
          {/* MIDIバーと5線譜を並べて置き、同じ区間を同時に見ながら補正する。 */}
          <MidiBar notes={notes} window={barWindow} />
          {notes.length === 0 && (
            <p className="text-sm text-gray-500 dark:text-gray-400">
              先頭{shownBars}小節に音符がありません。
            </p>
          )}
          <ScorePreview
            projectId={projectId}
            score={score}
            selectedNoteIds={NO_SELECTION}
            fromBar={1}
            toBar={barWindow.toBar}
            showHeading={false}
            // 5線譜は1本の横方向の段に並べる(楽譜清書ソフトと同じ見え方)。
            singleHorizontalStaffline
            zoom={zoom}
          />
        </div>
      )}
    </section>
  );
}
