/**
 * ビートグリッド補正の「現在値」と「適用後の値」の計算(#171)。
 *
 * 補正フォームは数値を入れて「適用」を押すまで何も起きないため、現在の設定が
 * どうなっているのかが画面から読み取れなかった。コントロールのすぐ隣に現在値を出し、
 * 入力中の値で**適用後どうなるか**を併記できるよう、必要な計算をここに集約する。
 */

import { type BeatSummary, formatTimeSignatureChanges } from "./beatSummary";

/** 全体オフセット適用後の拍の時刻(秒)。 */
export interface OffsetPreview {
  firstBeatSec: number;
  lastBeatSec: number;
}

/**
 * 全体オフセット(差分)を適用したときの、先頭と最後の拍の時刻。
 * 拍が無い、または差分が数値でない場合は`null`。
 */
export function offsetPreview(summary: BeatSummary, deltaSec: number): OffsetPreview | null {
  if (summary.firstBeatSec === null || summary.lastBeatSec === null) return null;
  if (!Number.isFinite(deltaSec)) return null;

  return {
    firstBeatSec: summary.firstBeatSec + deltaSec,
    lastBeatSec: summary.lastBeatSec + deltaSec,
  };
}

/** 固定BPMへ上書きしたときの拍間隔(秒)。0以下・非数値は`null`。 */
export function bpmOverrideIntervalSec(bpm: number): number | null {
  if (!Number.isFinite(bpm) || bpm <= 0) return null;
  return 60 / bpm;
}

/** 現在の代表テンポでの拍間隔(秒)。テンポが無ければ`null`。 */
export function currentBeatIntervalSec(summary: BeatSummary): number | null {
  if (summary.tempoBpm === null || summary.tempoBpm <= 0) return null;
  return 60 / summary.tempoBpm;
}

/**
 * 現在の拍子の割り当てを表す文言。
 *
 * 例: `4/4`、`4/4(小節 10・17 は 2/4)`。表記は②の検出結果パネルと同じ
 * `beatSummary.formatTimeSignatureChanges` に寄せる(同じ画面で2種類の書き分けを
 * しない。#173レビュー指摘)。
 */
export function currentMeterLabel(summary: BeatSummary): string {
  if (!summary.timeSignature) return "—";
  const changes = formatTimeSignatureChanges(summary);
  return changes ? `${summary.timeSignature}(${changes})` : summary.timeSignature;
}
