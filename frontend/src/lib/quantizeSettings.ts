/**
 * 量子化の適用設定(#174)。バックエンド(`worker/dsp_main.py`の
 * `quantize_min_value`/`quantize_strength`/`quantize_enabled`)と1対1で、
 * 既定は16分音符・強さ100%・有効。保存は表示小節数(#172)と同じくlocalStorage。
 */

/** 最小音符単位(= これより細かい格子へは寄せない)。 */
export type QuantizeMinValue = "1/4" | "1/8" | "1/16" | "1/32";

/** UIで選べる最小音符単位(粗い順)。 */
export const QUANTIZE_MIN_VALUE_CHOICES: readonly QuantizeMinValue[] = [
  "1/4",
  "1/8",
  "1/16",
  "1/32",
];

export const DEFAULT_QUANTIZE_MIN_VALUE: QuantizeMinValue = "1/16";

/** 強さ(0〜100%)。100で完全に格子へ寄せ、0で生の演奏位置のまま。 */
export const DEFAULT_QUANTIZE_STRENGTH_PERCENT = 100;

export const DEFAULT_QUANTIZE_ENABLED = true;

const QUANTIZE_SETTINGS_STORAGE_KEY = "ame.quantize";

export interface QuantizeSettings {
  minValue: QuantizeMinValue;
  strengthPercent: number;
  enabled: boolean;
}

export const DEFAULT_QUANTIZE_SETTINGS: QuantizeSettings = {
  minValue: DEFAULT_QUANTIZE_MIN_VALUE,
  strengthPercent: DEFAULT_QUANTIZE_STRENGTH_PERCENT,
  enabled: DEFAULT_QUANTIZE_ENABLED,
};

/** 未知の値(壊れた保存値)を選択肢のいずれかへ寄せる。 */
export function normalizeMinValue(value: unknown): QuantizeMinValue {
  return QUANTIZE_MIN_VALUE_CHOICES.includes(value as QuantizeMinValue)
    ? (value as QuantizeMinValue)
    : DEFAULT_QUANTIZE_MIN_VALUE;
}

/** 強さを0〜100の整数へ丸める(範囲外・非数は既定値)。 */
export function normalizeStrengthPercent(value: unknown): number {
  const number = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(number)) return DEFAULT_QUANTIZE_STRENGTH_PERCENT;
  return Math.min(100, Math.max(0, Math.round(number)));
}

/** 保存値全体を正規化する。 */
export function normalizeQuantizeSettings(value: unknown): QuantizeSettings {
  const record = (value ?? {}) as Record<string, unknown>;
  return {
    minValue: normalizeMinValue(record.minValue),
    strengthPercent: normalizeStrengthPercent(record.strengthPercent),
    enabled: typeof record.enabled === "boolean" ? record.enabled : DEFAULT_QUANTIZE_ENABLED,
  };
}

/** 保存された設定を読む(壊れていれば既定値)。 */
export function readStoredQuantizeSettings(storage: Storage): QuantizeSettings {
  const raw = storage.getItem(QUANTIZE_SETTINGS_STORAGE_KEY);
  if (!raw) return { ...DEFAULT_QUANTIZE_SETTINGS };
  try {
    return normalizeQuantizeSettings(JSON.parse(raw));
  } catch {
    // 保存値がJSONとして壊れている場合は既定へ戻す(例外でUIを落とさない)。
    return { ...DEFAULT_QUANTIZE_SETTINGS };
  }
}

/** 設定を保存する。 */
export function storeQuantizeSettings(storage: Storage, settings: QuantizeSettings): void {
  storage.setItem(
    QUANTIZE_SETTINGS_STORAGE_KEY,
    JSON.stringify(normalizeQuantizeSettings(settings)),
  );
}

/** 「16分音符」のような表示用ラベル。 */
export function minValueLabel(value: QuantizeMinValue): string {
  const denominator = value.slice(2);
  return `${denominator}分音符`;
}

/**
 * ジョブへ渡すパラメータ(バックエンドのキー名)。
 *
 * 強さだけ%から0〜1へ変換する。キー名はバックエンドの
 * `_quantize_settings_from_params` と一致していなければならない。
 */
export function toStageParams(settings: QuantizeSettings): Record<string, unknown> {
  const normalized = normalizeQuantizeSettings(settings);
  return {
    quantize_min_value: normalized.minValue,
    quantize_strength: normalized.strengthPercent / 100,
    quantize_enabled: normalized.enabled,
  };
}

/** 「16分音符・強さ100%・クオンタイズON」のような現在値の要約。 */
export function quantizeSettingsSummary(settings: QuantizeSettings): string {
  const normalized = normalizeQuantizeSettings(settings);
  const state = normalized.enabled ? "ON" : "OFF";
  if (!normalized.enabled) {
    // OFFのときは強さが効かないので、その旨だけを出す(誤解を避ける)。
    return `${minValueLabel(normalized.minValue)}・クオンタイズOFF(生の演奏位置)`;
  }
  return `${minValueLabel(normalized.minValue)}・強さ${normalized.strengthPercent}%・クオンタイズ${state}`;
}
