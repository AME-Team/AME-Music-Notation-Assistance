import { create } from "zustand";

interface MixState {
  muted: Set<string>;
  soloed: Set<string>;
  toggleMute: (name: string) => void;
  toggleSolo: (name: string) => void;
  /** ミュートされておらず、かつ(いずれかがソロ中なら)自身もソロ中であれば true。 */
  isAudible: (name: string) => boolean;
  reset: () => void;
}

/** #21 TrackList: ステムごとのソロ/ミュート状態。プロジェクト切替時に `reset()` する想定。 */
export const useMixStore = create<MixState>((set, get) => ({
  muted: new Set(),
  soloed: new Set(),

  toggleMute: (name) =>
    set((state) => {
      const next = new Set(state.muted);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return { muted: next };
    }),

  toggleSolo: (name) =>
    set((state) => {
      const next = new Set(state.soloed);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return { soloed: next };
    }),

  isAudible: (name) => {
    const { muted, soloed } = get();
    if (muted.has(name)) return false;
    if (soloed.size > 0) return soloed.has(name);
    return true;
  },

  reset: () => set({ muted: new Set(), soloed: new Set() }),
}));
