import { create } from "zustand";

export type PlaybackMode = "midi" | "audio";

interface PlaybackState {
  isPlaying: boolean;
  positionSec: number;
  positionTick: number;
  currentBar: number;
  bpm: number;
  mode: PlaybackMode;
  setMode: (mode: PlaybackMode) => void;
  reset: () => void;
}

const INITIAL_DEFAULTS = {
  isPlaying: false,
  positionSec: 0,
  positionTick: 0,
  currentBar: 1,
  bpm: 120,
} as const;

/**
 * TransportBar(#34)が唯一の書き込み元となる再生位置ストア。
 *
 * `PianoRoll.tsx`はプレイヘッド線を描くために既存の永続rAFループ内で
 * `usePlaybackStore.getState()`を直接読む(Reactの再レンダーを経由しない
 * `useMixStore`と同じ「高頻度更新はグローバルストア直読み」の既存方針)。
 * `ScorePreview.tsx`は`currentBar`/`isPlaying`のみを通常のReactフックで
 * 購読する(小節が変わる頻度は低いため素直な再レンダーで十分)。
 *
 * `mode`はA/B切替(MIDI/Audio)専用。`reset()`はTransportBarがプロジェクト
 * 切替時(`projectId`変更)に呼び、前のプロジェクトの再生位置を引き継がない
 * ようにする(`mode`はユーザー設定に近いため意図的にリセット対象外とする)。
 */
export const usePlaybackStore = create<PlaybackState>((set) => ({
  ...INITIAL_DEFAULTS,
  mode: "midi",

  setMode: (mode) => set({ mode }),

  reset: () => set({ ...INITIAL_DEFAULTS }),
}));
