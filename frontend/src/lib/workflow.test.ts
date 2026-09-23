import { describe, expect, it } from "vitest";
import type { Project } from "../api/client";
import { deriveStepStates, firstActionableStep } from "./workflow";

function makeProject(stages: Project["stages"]): Project {
  return {
    id: "p1",
    name: "song",
    original_filename: "song.wav",
    audio_format: "wav",
    created_at: new Date().toISOString(),
    stages,
  };
}

function stageOk(): Project["stages"][string] {
  return { status: "succeeded", progress: 1, stale: false };
}

function stageStale(): Project["stages"][string] {
  return { status: "succeeded", progress: 1, stale: true };
}

describe("deriveStepStates", () => {
  it("projectが未選択なら全ステップがlocked", () => {
    const states = deriveStepStates(undefined);
    expect(states.every((s) => s.status === "locked")).toBe(true);
  });

  it("新規プロジェクトは①だけがcurrent、以降はlocked", () => {
    const project = makeProject({});
    const states = deriveStepStates(project);
    expect(states.find((s) => s.id === "separate")?.status).toBe("current");
    expect(states.find((s) => s.id === "beat")?.status).toBe("locked");
    expect(states.find((s) => s.id === "export")?.status).toBe("locked");
  });

  it("順番に完了するとcurrentが先へ進む", () => {
    const project = makeProject({ separate: stageOk() });
    const states = deriveStepStates(project);
    expect(states.find((s) => s.id === "separate")?.status).toBe("done");
    expect(states.find((s) => s.id === "beat")?.status).toBe("current");
    expect(states.find((s) => s.id === "transcribe")?.status).toBe("locked");
  });

  it("上流がstaleだと、そのステップ以降がlockedになる(先へ進めない)", () => {
    const project = makeProject({
      separate: stageOk(),
      beat: stageOk(),
      transcribe: stageStale(),
      quantize: stageOk(),
    });
    const states = deriveStepStates(project);
    expect(states.find((s) => s.id === "transcribe")?.status).toBe("stale");
    expect(states.find((s) => s.id === "quantize")?.status).toBe("locked");
    expect(states.find((s) => s.id === "export")?.status).toBe("locked");
  });

  it("quantizeまで完了していれば、任意のrefineをスキップしてもreview/exportに進める", () => {
    const project = makeProject({
      separate: stageOk(),
      beat: stageOk(),
      transcribe: stageOk(),
      quantize: stageOk(),
    });
    const states = deriveStepStates(project, { refineSkipped: true });
    expect(states.find((s) => s.id === "refine")?.status).toBe("skipped");
    expect(states.find((s) => s.id === "review")?.status).toBe("current");
    expect(states.find((s) => s.id === "export")?.status).toBe("upcoming");
  });

  it("refineが未実行・未スキップならcurrentになり、review/exportはupcoming(到達可能だが未強調)のまま", () => {
    const project = makeProject({
      separate: stageOk(),
      beat: stageOk(),
      transcribe: stageOk(),
      quantize: stageOk(),
    });
    const states = deriveStepStates(project);
    expect(states.find((s) => s.id === "refine")?.status).toBe("current");
    expect(states.find((s) => s.id === "review")?.status).toBe("upcoming");
    // "current"はrefineの1つだけ(review/exportへ先送りしても二重にcurrentを立てない)。
    expect(states.filter((s) => s.status === "current")).toHaveLength(1);
  });
});

describe("firstActionableStep", () => {
  it("最初のcurrentステップを返す", () => {
    const project = makeProject({ separate: stageOk() });
    const states = deriveStepStates(project);
    expect(firstActionableStep(states)).toBe("beat");
  });
});
