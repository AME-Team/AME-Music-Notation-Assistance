/**
 * 波形ビューの表示区間(秒)を扱う純粋関数群(#169)。
 *
 * 波形を全曲分そのまま横幅へ圧縮すると、3分程度の曲では1拍が数ピクセルになり、
 * 波形を見ながら「全体オフセット」を決められない(実質使えない)。ここにズームと
 * 横スクロールの座標変換を集約し、**波形・ビート線・目盛りの3者が同じ変換を共有**
 * するようにする(片方だけ更新して重なりがズレる事故を防ぐ)。
 *
 * 表示区間の単位は秒とし、`BeatGridOverlay`の`viewBox`もこの区間で切り出す。
 */

/** 波形ビューの表示区間(秒)。 */
export interface TimeView {
  /** 表示開始(秒)。 */
  startSec: number;
  /** 表示終了(秒)。 */
  endSec: number;
}

/**
 * これ以上は拡大しない表示幅。1拍(120 BPMで0.5秒)より短くすると、
 * 拍どうしの位置関係が見えなくなり補正の役に立たない。
 */
export const MIN_VIEW_SPAN_SEC = 0.5;

/** 1操作(ボタン・ホイール)あたりの倍率。 */
export const ZOOM_STEP = 1.5;

/** 必要な解像度を見積もる際に想定する表示幅(px)。 */
export const ASSUMED_VIEWPORT_WIDTH_PX = 900;

/** 波形1点が担当する幅の目安(px)。これより粗いと拡大しても粒が目立つ。 */
export const TARGET_PIXELS_PER_POINT = 2;

/** 解像度の上限(点/秒)。10ms/点より細かくしても補正作業の役には立たない。 */
export const MAX_POINTS_PER_SEC = 100;

/**
 * 要求できる点数の上限。バックエンドの上限(`pipeline/peaks.py` の `MAX_BUCKETS`)と
 * **一致させる**(片方だけ変えると、422になるか要求できない段階が生まれる。#169レビュー指摘)。
 */
export const MAX_REQUESTED_BUCKETS = 40000;

/**
 * 拡大時に要求する点数の最小段階。既定解像度(1000)のすぐ上(1024など)を要求しても
 * 見た目が変わらないうえキャッシュファイルだけが増えるため、2の冪のうち2048から使う。
 */
export const MIN_REQUESTED_BUCKETS = 2048;

/** 既定の解像度(backend/app/pipeline/peaks.py の `DEFAULT_BUCKETS` と一致させる)。 */
export const DEFAULT_PEAKS_BUCKETS = 1000;

/** 全曲表示の区間。 */
export function fullView(durationSec: number): TimeView {
  return { startSec: 0, endSec: Math.max(durationSec, MIN_VIEW_SPAN_SEC) };
}

export function viewSpanSec(view: TimeView): number {
  return view.endSec - view.startSec;
}

/**
 * 全体表示を1.0倍とした倍率。
 *
 * `MIN_VIEW_SPAN_SEC`より短い素材(例: 0.1秒)では、全体表示でも表示幅が最小値に
 * なるため`durationSec / span`は1未満になる。画面の前提は「全体表示=1.0倍」なので、
 * 1.0を下回らせない(#169レビュー指摘)。
 */
export function zoomFactor(view: TimeView, durationSec: number): number {
  const span = viewSpanSec(view);
  if (span <= 0 || durationSec <= 0) return 1;
  return Math.max(1, durationSec / span);
}

/** 区間を「最小幅以上・曲の範囲内」に収める。 */
export function clampView(view: TimeView, durationSec: number): TimeView {
  const duration = Math.max(durationSec, MIN_VIEW_SPAN_SEC);
  const span = Math.min(Math.max(viewSpanSec(view), MIN_VIEW_SPAN_SEC), duration);
  const maxStart = Math.max(0, duration - span);
  const startSec = Math.min(Math.max(view.startSec, 0), maxStart);
  return { startSec, endSec: startSec + span };
}

/**
 * `focusSec`(ホイールズームではポインタ位置、ボタンでは表示中央)を画面内の同じ
 * 相対位置に保ったまま拡大/縮小する。`factor > 1` が拡大。
 */
export function zoomView(
  view: TimeView,
  factor: number,
  focusSec: number,
  durationSec: number,
): TimeView {
  const span = viewSpanSec(view);
  if (span <= 0 || factor <= 0) return clampView(view, durationSec);

  const focusRatio = Math.min(Math.max((focusSec - view.startSec) / span, 0), 1);
  const nextSpan = span / factor;
  const startSec = focusSec - focusRatio * nextSpan;
  return clampView({ startSec, endSec: startSec + nextSpan }, durationSec);
}

/** 横方向へ平行移動する(端では止まる)。 */
export function panView(view: TimeView, deltaSec: number, durationSec: number): TimeView {
  return clampView(
    { startSec: view.startSec + deltaSec, endSec: view.endSec + deltaSec },
    durationSec,
  );
}

/** 横スクロールバー(0..1)の現在位置。全体表示なら常に0。 */
export function panFraction(view: TimeView, durationSec: number): number {
  const maxStart = durationSec - viewSpanSec(view);
  if (maxStart <= 0) return 0;
  return Math.min(Math.max(view.startSec / maxStart, 0), 1);
}

/** 横スクロールバー(0..1)の位置と現在の表示幅から区間を作る(倍率は変えない)。 */
export function viewFromFraction(fraction: number, spanSec: number, durationSec: number): TimeView {
  const maxStart = Math.max(0, durationSec - spanSec);
  const startSec = Math.min(Math.max(fraction, 0), 1) * maxStart;
  return clampView({ startSec, endSec: startSec + spanSec }, durationSec);
}

/**
 * この表示幅で必要なピークの解像度(点の数)。
 *
 * 全曲表示では既定解像度のまま(バックエンドも従来のキャッシュファイルを再利用する)
 * にし、拡大したときだけ高解像度を要求する。既定解像度は1000点なので、3分の曲では
 * 1点が180msになり、拡大しても波形の粒が粗いまま＝拍の頭を目で合わせられない。
 */
export function requestedBuckets(view: TimeView, durationSec: number): number {
  if (durationSec <= 0) return DEFAULT_PEAKS_BUCKETS;

  const span = Math.max(viewSpanSec(view), MIN_VIEW_SPAN_SEC);
  const pointsPerSec = Math.min(
    MAX_POINTS_PER_SEC,
    ASSUMED_VIEWPORT_WIDTH_PX / span / TARGET_PIXELS_PER_POINT,
  );
  const wanted = Math.ceil(durationSec * pointsPerSec);
  if (wanted <= DEFAULT_PEAKS_BUCKETS) return DEFAULT_PEAKS_BUCKETS;

  // 連続値をそのまま使うと、ズームのたびに新しい解像度＝新しいキャッシュファイルが
  // 増え続ける(`{name}.{buckets}.json` は無効化まで残る)。2の冪へ切り上げて離散化し、
  // 解像度の選択肢を数個に収める。
  const discretized = 2 ** Math.max(11, Math.ceil(Math.log2(wanted)));
  return Math.min(MAX_REQUESTED_BUCKETS, discretized);
}

/** 波形1点が担当する時間(秒)。粒度表示に使う。 */
export function pointDurationSec(pointCount: number, durationSec: number): number {
  if (pointCount <= 0 || durationSec <= 0) return 0;
  return durationSec / pointCount;
}

/** 表示区間に含まれるピーク点の範囲(`[start, end)`)。無駄なDOMを作らないため切り出す。 */
export function visiblePointRange(
  pointCount: number,
  view: TimeView,
  durationSec: number,
): { start: number; end: number } {
  if (pointCount <= 0) return { start: 0, end: 0 };

  const pointSec = pointDurationSec(pointCount, durationSec);
  if (pointSec <= 0) return { start: 0, end: pointCount };

  const start = Math.min(Math.max(Math.floor(view.startSec / pointSec), 0), pointCount - 1);
  const end = Math.min(Math.max(Math.ceil(view.endSec / pointSec), start + 1), pointCount);
  return { start, end };
}

/** 目盛りの候補(秒)。 */
const TICK_STEPS_SEC = [0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300] as const;

/** 目盛り1本(位置は表示区間内の比率0..1)。 */
export interface RulerTick {
  timeSec: number;
  label: string;
  ratio: number;
}

/**
 * 表示幅に応じて間引いた目盛りを作る。ラベルが重ならないよう、最小間隔(px)を
 * 下回らない範囲で最も細かい刻みを選ぶ。
 */
export function rulerTicks(view: TimeView, widthPx: number, minSpacingPx = 72): RulerTick[] {
  const span = viewSpanSec(view);
  if (span <= 0 || widthPx <= 0 || minSpacingPx <= 0) return [];

  const maxTicks = Math.max(1, Math.floor(widthPx / minSpacingPx));
  const step =
    TICK_STEPS_SEC.find((candidate) => span / candidate <= maxTicks) ??
    TICK_STEPS_SEC[TICK_STEPS_SEC.length - 1];
  // ラベルは刻み幅の精度に合わせる(0.1秒刻みに「10.00」のような余分な桁を出さない)。
  const decimals = step >= 1 ? 0 : step >= 0.1 ? 1 : 2;

  const firstIndex = Math.ceil(view.startSec / step - 1e-9);
  const lastIndex = Math.floor(view.endSec / step + 1e-9);
  const ticks: RulerTick[] = [];
  for (let index = firstIndex; index <= lastIndex; index += 1) {
    // 加算を繰り返すと誤差が溜まるため、必ずインデックスから求める。
    const timeSec = Number((index * step).toFixed(3));
    ticks.push({
      timeSec,
      label: timeSec.toFixed(decimals),
      ratio: (timeSec - view.startSec) / span,
    });
  }
  return ticks;
}
