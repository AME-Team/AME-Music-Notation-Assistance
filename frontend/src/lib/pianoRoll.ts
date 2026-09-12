/**
 * PianoRoll(#30/#31)の座標変換・仮想化・ヒットテストを担う純粋関数群。
 *
 * `waveform.ts`/`beatGrid.ts`と同じ方針(描画コンポーネント本体から計算ロジックを
 * 剥がしてテスト可能にする)。Canvas描画自体・ドラッグ操作のイベントハンドリングは
 * `PianoRoll.tsx`側の責務。
 */

export interface TimeSignatureEntry {
  bar: number;
  numerator: number;
  denominator: number;
}

/** 編集対象になるノートの最小形状(`onset_tick`/`duration_tick`が未設定=量子化前

 * のノートは、tick空間で描画/編集できないため呼び出し側で除外してから渡すこと)。
 */
export interface PianoRollNote {
  id: number;
  onset_tick: number;
  duration_tick: number;
  midi: number;
  status: string;
  provenance: string;
  flags: string[];
}

/** #35: 出自(`provenance`)ごとの塗り/枠線パターン(設計書§12.4)。 */
export interface ProvenanceStyle {
  /** 塗り色。 */
  fill: string;
  /** 枠線色(塗り色より暗いトーン、非選択時も常に描く)。 */
  stroke: string;
  /** `CanvasRenderingContext2D.setLineDash()`にそのまま渡すダッシュパターン
   * (`[]`は実線)。色だけでなくパターンでも出自を区別できるようにする
   * (色覚特性への配慮、設計書§12.4の作業項目)。本番の色覚シミュレーション
   * ツールでの検証はこの環境では実施できないため、「色+パターンの2軸で
   * 区別できる」設計方針を満たすことをもって十分とする。
   */
  dash: number[];
  lineWidth: number;
}

const DEFAULT_PROVENANCE_STYLE: ProvenanceStyle = {
  fill: "#9ca3af",
  stroke: "#4b5563",
  dash: [],
  lineWidth: 1,
};

export const PROVENANCE_STYLE: Record<string, ProvenanceStyle> = {
  amt: DEFAULT_PROVENANCE_STYLE, // グレー実線: AMT生出力
  baseline: { fill: "#3b82f6", stroke: "#1e40af", dash: [], lineWidth: 1 }, // 青実線: L0決定論的整音済み
  llm: { fill: "#a855f7", stroke: "#6b21a8", dash: [2, 2], lineWidth: 1 }, // 紫点線: L1が変更(要レビュー)
  agent: { fill: "#d946ef", stroke: "#86198f", dash: [6, 2, 2, 2], lineWidth: 1 }, // マゼンタ破線点: L2が変更(要レビュー)
  user: { fill: "#f97316", stroke: "#9a3412", dash: [], lineWidth: 2 }, // オレンジ太実線: 手動編集済み
};

/** 未知の`provenance`値(スキーマ上あり得ないが、防御的に)は`amt`相当で描く。 */
export function provenanceStyle(provenance: string): ProvenanceStyle {
  return PROVENANCE_STYLE[provenance] ?? DEFAULT_PROVENANCE_STYLE;
}

/** `bar`時点で有効な拍子`(numerator, denominator)`。指定が無ければ4/4相当。

 * `backend/app/pipeline/time_signature.py`の`time_signature_at_bar`と同じ
 * ロジック(直近の変化点を順方向に補完)のTS移植。`time_signatures`は`bar`昇順
 * に並んでいる前提(Score IR/beatmap.jsonの契約)。
 */
export function timeSignatureAtBar(
  timeSignatures: TimeSignatureEntry[],
  bar: number,
): [numerator: number, denominator: number] {
  let numerator = 4;
  let denominator = 4;
  for (const ts of timeSignatures) {
    if (ts.bar <= bar) {
      numerator = ts.numerator;
      denominator = ts.denominator;
    } else {
      break;
    }
  }
  return [numerator, denominator];
}

const MAX_BARS_SAFETY_LIMIT = 100_000;

/**
 * 小節境界のtick列(`[0, 小節2開始tick, 小節3開始tick, ...]`)を、`lastTick`を
 * 覆うまで計算する。拍子変化にも対応する。
 *
 * `numerator <= 0`のような不正な拍子データでも無限ループしないよう、
 * 安全弁として小節数の上限を設ける。
 */
export function barBoundariesTicks(
  timeSignatures: TimeSignatureEntry[],
  divisions: number,
  lastTick: number,
): number[] {
  const boundaries = [0];
  let tick = 0;
  let bar = 1;
  while (tick < lastTick && bar <= MAX_BARS_SAFETY_LIMIT) {
    const [numerator, denominator] = timeSignatureAtBar(timeSignatures, bar);
    const ticksPerBar = (numerator * divisions * 4) / denominator;
    if (ticksPerBar <= 0) break;
    tick += ticksPerBar;
    boundaries.push(tick);
    bar += 1;
  }
  return boundaries;
}

/**
 * #33: `tick`が何小節目(1始まり)にあるかを求める(`barBoundariesTicks`の
 * 出力に対する二分探索)。`ScorePreview`が選択ノートの小節番号を求めて
 * OSMDの表示範囲を自動追従させるのに使う。
 */
export function barNumberForTick(boundaries: number[], tick: number): number {
  let lo = 0;
  let hi = boundaries.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (boundaries[mid] <= tick) lo = mid + 1;
    else hi = mid;
  }
  return Math.max(1, lo);
}

export function tickToX(tick: number, scrollTick: number, pxPerTick: number): number {
  return (tick - scrollTick) * pxPerTick;
}

export function xToTick(x: number, scrollTick: number, pxPerTick: number): number {
  return x / pxPerTick + scrollTick;
}

/** `topMidi`はビューポート上端(y=0)に表示されるMIDIノート番号。 */
export function midiToY(midi: number, topMidi: number, rowHeightPx: number): number {
  return (topMidi - midi) * rowHeightPx;
}

export function yToMidi(y: number, topMidi: number, rowHeightPx: number): number {
  return topMidi - y / rowHeightPx;
}

/**
 * 可視tick範囲と重なりうるノートの`[startIndex, endIndex)`を、`onset_tick`昇順
 * ソート済み配列に対する二分探索で求める(#30: ビューポート外は走査しない)。
 *
 * 正確性のための注意: あるノートが可視範囲と重なるかは`onset_tick`だけでなく
 * `onset_tick + duration_tick`(終了位置)にも依存するため、`onset_tick`のみで
 * ソートされた配列への二分探索だけでは、可視範囲開始より前から鳴り続ける
 * 長いノートを取りこぼしうる。これを厳密に解くには終了tickでも索引した
 * 区間木が必要になるが、ピアノロールの実用上ほとんどのノートは高々数小節分
 * (せいぜい`VISIBLE_RANGE_LOOKBACK_TICKS`)の長さに収まるため、開始位置の
 * 二分探索対象を`viewStartTick - VISIBLE_RANGE_LOOKBACK_TICKS`まで広げることで
 * 実用上十分な精度で仮想化する(極端に長いノート1本を稀に見逃す可能性はある
 * トレードオフとして許容する)。
 */
const VISIBLE_RANGE_LOOKBACK_TICKS = 480 * 4 * 16; // 480tick/拍・4拍/小節×16小節分

export function visibleNoteRange(
  sortedNotes: PianoRollNote[],
  viewStartTick: number,
  viewEndTick: number,
): [startIndex: number, endIndex: number] {
  const lowerBound = lowerBoundByOnsetTick(
    sortedNotes,
    viewStartTick - VISIBLE_RANGE_LOOKBACK_TICKS,
  );
  const upperBound = upperBoundByOnsetTick(sortedNotes, viewEndTick);
  return [lowerBound, Math.max(lowerBound, upperBound)];
}

function lowerBoundByOnsetTick(notes: PianoRollNote[], target: number): number {
  let lo = 0;
  let hi = notes.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (notes[mid].onset_tick < target) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

function upperBoundByOnsetTick(notes: PianoRollNote[], target: number): number {
  let lo = 0;
  let hi = notes.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (notes[mid].onset_tick <= target) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/** #35: `"restore"`は削除済みノートのクリックで復活させる操作(設計書§12.4
 * 「クリックで復活」)。ドラッグ(移動/リサイズ)の対象にはならない。
 */
export type HitRegion = "move" | "resize-right" | "restore";

export interface NoteHit {
  note: PianoRollNote;
  region: HitRegion;
}

/** ノート右端付近(ドラッグでリサイズするための当たり判定領域)の幅(px単位)。 */
const RESIZE_HANDLE_WIDTH_PX = 8;

/**
 * 指定したtick/midi位置にあるノートを探す(#30: ヒットテスト)。`sortedNotes`は
 * `visibleNoteRange`で絞り込んだ後の(可視範囲付近の)配列を渡す想定。
 * 複数重なる場合は最後に描画される(=配列の後方にある)ノートを優先する。
 *
 * `status === "deleted"`のノートも対象に含める(#35: クリックで復活)。
 * `"muted"`(現状どのパイプラインも設定しない)は#30時点から一貫して対象外
 * のまま維持する。
 */
export function hitTestNote(
  sortedNotes: PianoRollNote[],
  tick: number,
  midi: number,
  pxPerTick: number,
): NoteHit | null {
  for (let i = sortedNotes.length - 1; i >= 0; i -= 1) {
    const note = sortedNotes[i];
    if (note.status === "muted") continue;
    if (Math.round(note.midi) !== Math.round(midi)) continue;
    const end = note.onset_tick + note.duration_tick;
    if (tick < note.onset_tick || tick > end) continue;
    if (note.status === "deleted") {
      return { note, region: "restore" };
    }
    // #30-M3レビュー指摘: handleWidthを固定px幅のままにすると、低ズーム
    // (pxPerTickが小さい)時や短いノートでハンドル幅がノート全体を覆って
    // しまい、"move"領域が消えて移動操作が不能になる。ノート幅の半分を
    // 上限にクランプし、pxPerTick<=0(呼び出し側の不正値)でも
    // Infinity/NaNにならないよう安全側に倒す。
    const handleWidthTicks =
      pxPerTick > 0 ? Math.min(RESIZE_HANDLE_WIDTH_PX / pxPerTick, note.duration_tick / 2) : 0;
    const region: HitRegion = tick >= end - handleWidthTicks ? "resize-right" : "move";
    return { note, region };
  }
  return null;
}
