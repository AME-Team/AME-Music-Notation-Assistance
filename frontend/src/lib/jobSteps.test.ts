import { describe, expect, it } from "vitest";
import { stageLabel, stepLabel } from "./jobSteps";

describe("stageLabel", () => {
  it("既知のstageを日本語に変換する", () => {
    expect(stageLabel("transcribe")).toBe("採譜");
    expect(stageLabel("quantize")).toBe("リズム補正");
  });

  it("未知のstageはそのまま返す", () => {
    expect(stageLabel("unknown_stage")).toBe("unknown_stage");
  });

  it("未指定は「処理」を返す", () => {
    expect(stageLabel(undefined)).toBe("処理");
  });
});

describe("stepLabel", () => {
  it("既知のstage+stepを日本語に変換する", () => {
    expect(stepLabel("transcribe", "piano")).toBe("ピアノを採譜中");
    expect(stepLabel("quantize", "save")).toBe("結果を保存中");
  });

  it("未知のstepはキーをそのまま返す", () => {
    expect(stepLabel("transcribe", "mystery")).toBe("mystery");
  });

  it("stageまたはstepが無ければnullを返す", () => {
    expect(stepLabel(undefined, "piano")).toBeNull();
    expect(stepLabel("transcribe", undefined)).toBeNull();
  });
});
