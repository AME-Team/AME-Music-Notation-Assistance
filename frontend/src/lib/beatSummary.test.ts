import { describe, expect, it } from "vitest";
import type { Beatmap } from "../api/client";
import {
  type BeatTableRow,
  formatBeatPosition,
  formatBpm,
  formatSeconds,
  formatTempoNote,
  formatTimeSignatureChanges,
  MAX_VISIBLE_ROWS,
  summarizeBeatmap,
  TEMPO_STABLE_TOLERANCE_BPM,
} from "./beatSummary";

type BeatEntry = Beatmap["beats"][number];

/** 指定の小節数・1小節あたりの拍数を持つビート列(等間隔)を作る。 */
function makeBeats(bars: number, beatsPerBar: number, intervalSec = 0.5): BeatEntry[] {
  const beats: BeatEntry[] = [];
  let index = 0;
  for (let bar = 1; bar <= bars; bar += 1) {
    for (let beat = 1; beat <= beatsPerBar; beat += 1) {
      beats.push({ time_sec: Number((index * intervalSec).toFixed(2)), beat_in_bar: beat, bar });
      index += 1;
    }
  }
  return beats;
}

/**
 * `_tempo_map`と同じ形(先頭の拍を除く各拍に、直前の拍からの瞬間BPM)を作る。
 * `bpm`に関数を渡すと拍ごとに値を変えられる。
 */
function makeTempoMap(beats: BeatEntry[], bpm: number | ((index: number) => number)) {
  return beats.slice(1).map((beat, index) => ({
    bar: beat.bar,
    beat: beat.beat_in_bar,
    bpm: typeof bpm === "function" ? bpm(index) : bpm,
  }));
}

function makeBeatmap(override: Partial<Beatmap> = {}): Beatmap {
  const beats = makeBeats(3, 4);
  return {
    beats,
    downbeats_sec: [0, 2, 4],
    time_signatures: [{ bar: 1, numerator: 4, denominator: 4 }],
    tempo_map: makeTempoMap(beats, 120),
    confidence: 0.9928,
    source: "auto",
    ...override,
  };
}

describe("summarizeBeatmap", () => {
  it("拍数・小節数・ダウンビート数・先頭/最後の拍・平均拍間隔を集計する", () => {
    const summary = summarizeBeatmap(makeBeatmap());

    expect(summary.beatCount).toBe(12);
    expect(summary.barCount).toBe(3);
    expect(summary.downbeatCount).toBe(3);
    expect(summary.firstBeatSec).toBe(0);
    expect(summary.lastBeatSec).toBe(5.5);
    // (5.5 - 0) / (12 - 1) = 0.5
    expect(summary.beatIntervalSec).toBeCloseTo(0.5, 6);
  });

  it("代表テンポは瞬間BPMの中央値で、幅が小さければ「ほぼ一定」と判定する", () => {
    const summary = summarizeBeatmap(makeBeatmap());

    expect(summary.tempoBpm).toBe(120);
    expect(summary.tempoMinBpm).toBe(120);
    expect(summary.tempoMaxBpm).toBe(120);
    expect(summary.tempoIsStable).toBe(true);
    expect(formatTempoNote(summary)).toBe("ほぼ一定");
  });

  it("テンポが動く曲では幅を示し、揺れの許容を超えたら一定と見なさない", () => {
    const beats = makeBeats(3, 4);
    // 115.38〜125.00(実データ相当)。偶数のため中央値は中央2つの平均。
    const bpms = [115.38, 125.0, 118.0, 120.0];
    const summary = summarizeBeatmap(
      makeBeatmap({ beats, tempo_map: makeTempoMap(beats, (i) => bpms[i % bpms.length]) }),
    );

    expect(summary.tempoMinBpm).toBe(115.38);
    expect(summary.tempoMaxBpm).toBe(125);
    expect(summary.tempoIsStable).toBe(false);
    const tempoSpread = (summary.tempoMaxBpm ?? 0) - (summary.tempoMinBpm ?? 0);
    expect(tempoSpread).toBeGreaterThan(TEMPO_STABLE_TOLERANCE_BPM);
    expect(formatTempoNote(summary)).toBe("曲中で変動(115.4〜125.0 BPM)");
  });

  it("中央値は要素が偶数のとき中央2つの平均になる", () => {
    const beats = makeBeats(2, 4);
    const summary = summarizeBeatmap(
      makeBeatmap({
        beats,
        // tempo_mapはエントリ4件(偶数)。中央2つは100と200なので中央値は150。
        tempo_map: [
          { bar: 1, beat: 2, bpm: 100 },
          { bar: 1, beat: 3, bpm: 100 },
          { bar: 1, beat: 4, bpm: 200 },
          { bar: 2, beat: 1, bpm: 200 },
        ],
      }),
    );

    expect(summary.tempoBpm).toBe(150);
  });

  it("支配的な拍子は「その拍子で書かれた小節数」で選び、異なる拍子の小節を列挙する", () => {
    // 実データ(proj_e2e60)と同じ並び: 4/4が15小節(1-9, 11-16)、2/4が2小節(10, 17)
    const beats = makeBeats(17, 4);
    const summary = summarizeBeatmap(
      makeBeatmap({
        beats,
        time_signatures: [
          { bar: 1, numerator: 4, denominator: 4 },
          { bar: 10, numerator: 2, denominator: 4 },
          { bar: 11, numerator: 4, denominator: 4 },
          { bar: 17, numerator: 2, denominator: 4 },
        ],
      }),
    );

    expect(summary.timeSignature).toBe("4/4");
    expect(summary.timeSignatureChanges).toEqual([
      { bar: 10, numerator: 2, denominator: 4 },
      { bar: 17, numerator: 2, denominator: 4 },
    ]);
    expect(formatTimeSignatureChanges(summary)).toBe("小節 10・17 は 2/4");
  });

  it("ピックアップ拍(bar=0)は数えて区別し、位置表示は「—」にする", () => {
    const pickup: BeatEntry = { time_sec: 0.04, beat_in_bar: 0, bar: 0 };
    const beats = [pickup, ...makeBeats(1, 4)];
    const summary = summarizeBeatmap(
      makeBeatmap({ beats, downbeats_sec: [0.54], tempo_map: makeTempoMap(beats, 120) }),
    );

    expect(summary.beatCount).toBe(5);
    expect(summary.pickupBeatCount).toBe(1);
    expect(summary.barCount).toBe(1);
    expect(summary.rows[0].bar).toBe(0);
    expect(formatBeatPosition(summary.rows[0])).toBe("—");
    expect(formatBeatPosition(summary.rows[1])).toBe("1.1");
    // ピックアップ拍の位置は`tempo_map`に存在しないためBPMはnull。
    expect(summary.rows[0].bpm).toBeNull();
  });

  it("拍の一覧は小節・小節内の拍で瞬間BPMを引き当てる(先頭の拍はnull)", () => {
    const summary = summarizeBeatmap(makeBeatmap());

    expect(summary.rows).toHaveLength(12);
    expect(summary.rows[0]).toMatchObject({ index: 1, bar: 1, beatInBar: 1, bpm: null });
    expect(summary.rows[1]).toMatchObject({ index: 2, bar: 1, beatInBar: 2, bpm: 120 });
    expect(summary.rows[11]).toMatchObject({ index: 12, bar: 3, beatInBar: 4, bpm: 120 });
  });

  it("空データでも例外を出さずnull/0を返す", () => {
    const summary = summarizeBeatmap(
      makeBeatmap({
        beats: [],
        downbeats_sec: [],
        time_signatures: [],
        tempo_map: [],
        confidence: 0,
      }),
    );

    expect(summary.beatCount).toBe(0);
    expect(summary.barCount).toBe(0);
    expect(summary.firstBeatSec).toBeNull();
    expect(summary.lastBeatSec).toBeNull();
    expect(summary.tempoBpm).toBeNull();
    expect(summary.tempoIsStable).toBe(false);
    expect(summary.beatIntervalSec).toBeNull();
    expect(summary.timeSignature).toBeNull();
    expect(summary.timeSignatureChanges).toEqual([]);
    expect(summary.rows).toEqual([]);
    expect(formatTempoNote(summary)).toBe("テンポを推定できませんでした");
    expect(formatTimeSignatureChanges(summary)).toBeNull();
  });

  it("拍が1つだけなら平均拍間隔は算出しない", () => {
    const beats: BeatEntry[] = [{ time_sec: 0, beat_in_bar: 1, bar: 1 }];
    const summary = summarizeBeatmap(
      makeBeatmap({ beats, downbeats_sec: [0], tempo_map: makeTempoMap(beats, 120) }),
    );

    expect(summary.beatIntervalSec).toBeNull();
    expect(summary.firstBeatSec).toBe(0);
    expect(summary.lastBeatSec).toBe(0);
  });

  it("tempo_mapが空なら代表テンポはnullで、一覧のBPMも全てnullにする", () => {
    const summary = summarizeBeatmap(makeBeatmap({ tempo_map: [] }));

    expect(summary.tempoBpm).toBeNull();
    expect(summary.tempoMinBpm).toBeNull();
    expect(summary.tempoMaxBpm).toBeNull();
    expect(summary.rows.every((row) => row.bpm === null)).toBe(true);
  });

  it("信頼度は0〜100の整数に丸め、範囲外やNaNは0〜100に収める", () => {
    expect(summarizeBeatmap(makeBeatmap()).confidencePercent).toBe(99);
    expect(summarizeBeatmap(makeBeatmap({ confidence: 0 })).confidencePercent).toBe(0);
    expect(summarizeBeatmap(makeBeatmap({ confidence: 1.5 })).confidencePercent).toBe(100);
    expect(summarizeBeatmap(makeBeatmap({ confidence: -0.5 })).confidencePercent).toBe(0);
    expect(summarizeBeatmap(makeBeatmap({ confidence: Number.NaN })).confidencePercent).toBe(0);
  });

  it("拍子エントリが無くても小節数は拍から数える", () => {
    const summary = summarizeBeatmap(makeBeatmap({ time_signatures: [] }));

    expect(summary.timeSignature).toBeNull();
    expect(summary.barCount).toBe(3);
  });
});

describe("整形ヘルパ", () => {
  it("BPMと秒は桁数を固定し、値が無ければ「—」を返す", () => {
    expect(formatBpm(120)).toBe("120.0");
    expect(formatBpm(115.376)).toBe("115.4");
    expect(formatBpm(null)).toBe("—");
    expect(formatSeconds(0.04)).toBe("0.04 秒");
    expect(formatSeconds(null)).toBe("—");
  });

  it("拍位置は「小節.拍」、ピックアップ拍は「—」", () => {
    const row: BeatTableRow = {
      index: 1,
      bar: 2,
      beatInBar: 3,
      timeSec: 1,
      bpm: 120,
    };
    expect(formatBeatPosition(row)).toBe("2.3");
    expect(formatBeatPosition({ ...row, bar: 0, beatInBar: 0 })).toBe("—");
    // 小節はあるが拍番号が0(欠損)の場合も位置として成立しないため「—」。
    expect(formatBeatPosition({ ...row, beatInBar: 0 })).toBe("—");
  });

  it("初期表示行数は0より大きい", () => {
    expect(MAX_VISIBLE_ROWS).toBeGreaterThan(0);
  });
});
