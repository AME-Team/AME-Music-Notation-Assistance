import { describe, expect, it } from "vitest";
import type { ScoreIR } from "../api/client";
import {
  DEFAULT_PREVIEW_BARS,
  DEFAULT_SCORE_LAYOUT,
  DEFAULT_ZOOM,
  midiBarRect,
  normalizePreviewBars,
  normalizeScoreLayout,
  normalizeZoom,
  previewNotes,
  previewPitchRange,
  previewUnavailableReason,
  previewWindow,
  previewWindowResult,
  readStoredPreviewBars,
  readStoredScoreLayout,
  readStoredZoom,
  storePreviewBars,
  storeScoreLayout,
  storeZoom,
  ZOOM_MAX,
  ZOOM_MIN,
  ZOOM_STEP,
} from "./scoreView";

/** 4/4・divisions=480(1拍=480tick、1小節=1920tick)の最小スコア。 */
function makeScore(): ScoreIR {
  return {
    schema_version: 1,
    project_id: "proj",
    source: { filename: "a.wav", duration_sec: 10, sample_rate: 44100 },
    divisions: 480,
    tempo_map: [{ bar: 1, beat: 1, bpm: 120 }],
    time_signatures: [{ bar: 1, numerator: 4, denominator: 4 }],
    key_signatures: [],
    chords: [],
    parts: [
      {
        id: "piano",
        name: "Piano",
        notes: [
          { id: 1, midi: 60, onset_tick: 0, duration_tick: 480 },
          { id: 2, midi: 62, onset_tick: 480, duration_tick: 480 },
          // 2小節目
          { id: 3, midi: 64, onset_tick: 1920, duration_tick: 480 },
          // 3小節目
          { id: 4, midi: 65, onset_tick: 3840, duration_tick: 480 },
        ],
      },
    ],
    meta: { stages: {} },
    next_note_id: 5,
  } as unknown as ScoreIR;
}

describe("normalizePreviewBars", () => {
  it("選択肢にある値はそのまま、無い値は既定へ落とす", () => {
    expect(normalizePreviewBars(8)).toBe(8);
    expect(normalizePreviewBars("16")).toBe(16);
    expect(normalizePreviewBars(3)).toBe(DEFAULT_PREVIEW_BARS);
    expect(normalizePreviewBars(0)).toBe(DEFAULT_PREVIEW_BARS);
    expect(normalizePreviewBars("abc")).toBe(DEFAULT_PREVIEW_BARS);
    expect(normalizePreviewBars(null)).toBe(DEFAULT_PREVIEW_BARS);
  });

  it("保存値の読み書きが往復する(壊れた値は既定へ)", () => {
    const storage = new Map<string, string>();
    const fake = {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => void storage.set(key, value),
    } as unknown as Storage;

    storePreviewBars(fake, 32);
    expect(readStoredPreviewBars(fake)).toBe(32);

    storage.set("ame.scoreView.bars", "999");
    expect(readStoredPreviewBars(fake)).toBe(DEFAULT_PREVIEW_BARS);
  });
});

describe("previewWindow", () => {
  it("先頭N小節のtick範囲を返す(4/4・480divisions)", () => {
    const window = previewWindow(makeScore(), 2);
    expect(window).not.toBeNull();
    expect(window?.toBar).toBe(2);
    expect(window?.fromTick).toBe(0);
    expect(window?.toTick).toBe(3840);
  });

  it("曲より大きい小節数を指定しても、実在する小節までに丸める", () => {
    expect(previewWindow(makeScore(), 100)?.toBar).toBe(3);
  });

  it("作れない理由を区別して返す(量子化前/分解能不正/曲が短い)", () => {
    const score = makeScore();
    // 量子化前はonset_tickが無い = 音符0件と同じ扱い。
    expect(previewWindow({ ...score, parts: [] }, 4)).toBeNull();
    expect(previewUnavailableReason({ ...score, parts: [] }, 4)).toBe("no-notes");
    expect(previewUnavailableReason({ ...score, divisions: 0 }, 4)).toBe("invalid-divisions");
    // 1小節に満たないスコア。
    expect(
      previewUnavailableReason(
        {
          divisions: 480,
          time_signatures: [{ bar: 1, numerator: 4, denominator: 4 }],
          parts: [{ notes: [] }],
        } as never,
        4,
      ),
    ).toBe("no-notes");
    // 作れるときは理由はnull。
    expect(previewUnavailableReason(score, 4)).toBeNull();
  });

  it("previewWindowResult は範囲か理由のどちらかを1回の解析で返す", () => {
    const score = makeScore();
    const ok = previewWindowResult(score, 2);
    expect("window" in ok).toBe(true);
    expect("window" in ok && ok.window.toBar).toBe(2);
    // previewWindow / previewUnavailableReason と同じ結論になる。
    expect("window" in ok && ok.window).toEqual(previewWindow(score, 2));

    const ng = previewWindowResult({ ...score, parts: [] }, 2);
    expect("reason" in ng && ng.reason).toBe(previewUnavailableReason({ ...score, parts: [] }, 2));
    expect("reason" in ng && ng.reason).toBe("no-notes");
  });
});

describe("previewNotes", () => {
  it("範囲内の音符だけを返し、はみ出す長さは切り詰める", () => {
    const score = makeScore();
    const window = previewWindow(score, 2);
    if (!window) throw new Error("window");

    const notes = previewNotes(score, window);
    expect(notes.map((note) => note.midi)).toEqual([60, 62, 64]);

    // 2小節目を跨ぐ音符は範囲の終わりで切る。
    const crossing = previewNotes(
      {
        parts: [{ notes: [{ id: 99, midi: 70, onset_tick: 1440, duration_tick: 3840 }] }],
      } as never,
      window,
    );
    expect(crossing[0].durationTick).toBe(window.toTick - 1440);
  });
});

describe("midiBarRect", () => {
  it("Xはtick、Yは音高に対応し、最小の幅・高さを保証する", () => {
    const score = makeScore();
    const window = previewWindow(score, 2);
    if (!window) throw new Error("window");
    const notes = previewNotes(score, window);
    const range = previewPitchRange(notes);
    if (!range) throw new Error("range");

    const size = { width: 1000, height: 100 };
    const first = midiBarRect(notes[0], window, range, size);
    // 先頭の音符は左端、長さ1拍ぶん。
    expect(first.x).toBe(0);
    expect(first.width).toBeCloseTo(125, 6);
    // 音高が高いほどYは小さい(上へ描く)。
    const rangeWithHigh = { minMidi: 60, maxMidi: 72 };
    const low = midiBarRect(
      { id: 1, midi: 60, onsetTick: 0, durationTick: 480 },
      window,
      rangeWithHigh,
      size,
    );
    const high = midiBarRect(
      { id: 2, midi: 72, onsetTick: 0, durationTick: 480 },
      window,
      rangeWithHigh,
      size,
    );
    expect(high.y).toBeLessThan(low.y);

    // 極端に短い音符でも幅1px・高さ1pxは確保する。
    const tiny = midiBarRect(
      { id: 1, midi: 60, onsetTick: 0, durationTick: 1 },
      window,
      { minMidi: 60, maxMidi: 84 },
      size,
    );
    expect(tiny.width).toBeGreaterThanOrEqual(1);
    expect(tiny.height).toBeGreaterThanOrEqual(1);
  });

  it("音符が無ければ音高範囲は null", () => {
    expect(previewPitchRange([])).toBeNull();
  });
});

describe("normalizeZoom (#179)", () => {
  it("扱える範囲へ寄せて小数1桁に丸める", () => {
    expect(normalizeZoom(1)).toBe(1);
    expect(normalizeZoom("1.2")).toBe(1.2);
    expect(normalizeZoom(1.2345)).toBe(1.2);
    // 0.2刻みの増減で二進小数の誤差を蓄積させない。
    expect(normalizeZoom(1 + ZOOM_STEP * 3)).toBe(1.6);
  });

  it("範囲外は下限・上限へ寄せる", () => {
    expect(normalizeZoom(0.01)).toBe(ZOOM_MIN);
    expect(normalizeZoom(99)).toBe(ZOOM_MAX);
  });

  it("下限から増やすと既定の100%へ到達できる(#179レビュー指摘)", () => {
    // 下限が増減幅の格子に乗っていないと(例:0.5)、0.5→0.7→0.9→1.1…と進み
    // 既定の100%へ二度と戻れなくなる。
    let zoom = normalizeZoom(0.01); // 下限
    for (let i = 0; i < 10 && zoom !== DEFAULT_ZOOM; i += 1) {
      zoom = normalizeZoom(zoom + ZOOM_STEP);
    }

    expect(zoom).toBe(DEFAULT_ZOOM);
  });

  it("数値にならない値は既定へ落とす", () => {
    expect(normalizeZoom("abc")).toBe(DEFAULT_ZOOM);
    expect(normalizeZoom(null)).toBe(DEFAULT_ZOOM);
  });
});

describe("readStoredZoom / storeZoom (#179)", () => {
  function makeStorage(initial: Record<string, string> = {}): Storage {
    const map = new Map(Object.entries(initial));
    return {
      getItem: (key: string) => map.get(key) ?? null,
      setItem: (key: string, value: string) => {
        map.set(key, value);
      },
      length: map.size,
    } as unknown as Storage;
  }

  it("保存して読み戻せる", () => {
    const storage = makeStorage();
    storeZoom(storage, 1.4);

    expect(storage.getItem("ame.scoreView.zoom")).toBe("1.4");
    expect(readStoredZoom(storage)).toBe(1.4);
  });

  it("壊れた保存値は既定へ落とす", () => {
    expect(readStoredZoom(makeStorage({ "ame.scoreView.zoom": "x" }))).toBe(DEFAULT_ZOOM);
    expect(readStoredZoom(makeStorage({ "ame.scoreView.zoom": "100" }))).toBe(ZOOM_MAX);
    expect(readStoredZoom(makeStorage())).toBe(DEFAULT_ZOOM);
  });
});

describe("normalizeScoreLayout / 保存 (#181)", () => {
  function makeStorage(initial: Record<string, string> = {}): Storage {
    const map = new Map(Object.entries(initial));
    return {
      getItem: (key: string) => map.get(key) ?? null,
      setItem: (key: string, value: string) => {
        map.set(key, value);
      },
      length: map.size,
    } as unknown as Storage;
  }

  it("既定は横に1段(#179の要求)", () => {
    expect(DEFAULT_SCORE_LAYOUT).toBe("single-line");
    expect(normalizeScoreLayout(null)).toBe("single-line");
    expect(normalizeScoreLayout("なにか")).toBe("single-line");
  });

  it("保存して読み戻せる", () => {
    const storage = makeStorage();
    storeScoreLayout(storage, "wrap");

    expect(storage.getItem("ame.scoreView.layout")).toBe("wrap");
    expect(readStoredScoreLayout(storage)).toBe("wrap");
    expect(readStoredScoreLayout(makeStorage({ "ame.scoreView.layout": "x" }))).toBe("single-line");
  });
});

describe("縮尺スライダーの刻みと既定値の整合 (#181レビュー指摘)", () => {
  // rangeの有効値は min + n*step で決まる。既定の100%がその格子に乗っていないと、
  // つまみが勝手な値へスナップし、初回ドラッグで意図しない倍率へ飛ぶ。
  it("既定の100%と上下限が刻みの格子に乗る", () => {
    const onGrid = (value: number) => {
      const steps = (value - ZOOM_MIN) / ZOOM_STEP;
      return Math.abs(steps - Math.round(steps)) < 1e-9;
    };

    expect(onGrid(DEFAULT_ZOOM)).toBe(true);
    expect(onGrid(ZOOM_MAX)).toBe(true);
  });

  it("格子の値は丸めで動かない", () => {
    for (let value = ZOOM_MIN; value <= ZOOM_MAX + 1e-9; value += ZOOM_STEP) {
      expect(normalizeZoom(value)).toBeCloseTo(value, 9);
    }
    expect(normalizeZoom(DEFAULT_ZOOM)).toBe(DEFAULT_ZOOM);
  });
});
