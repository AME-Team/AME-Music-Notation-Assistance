import type { Project } from "../api/client";

export type StepId =
  | "separate"
  | "beat"
  | "transcribe"
  | "quantize"
  | "refine"
  | "review"
  | "export";

export interface StepDef {
  id: StepId;
  /** サイドバー・見出しに出す短い名前(番号なし。番号はUI側で付ける)。 */
  title: string;
  /** ステップ画面の説明文(1〜2行)。 */
  description: string;
  /** 対応するDSPジョブのstageキー。無ければ(refine/review/export)DSPジョブを持たない。 */
  stage?: string;
  /** 任意工程(スキップ可能)かどうか。 */
  optional?: boolean;
}

export const STEPS: readonly StepDef[] = [
  {
    id: "separate",
    title: "音源分離",
    description: "曲の音源を、ピアノ・ベース・ボーカルなど楽器ごとの音に分離します。",
    stage: "separate",
  },
  {
    id: "beat",
    title: "テンポ・拍の検出",
    description: "曲のテンポと拍・小節の位置を推定します。以降のすべての工程の基準になります。",
    stage: "beat",
  },
  {
    id: "transcribe",
    title: "採譜",
    description: "分離した各楽器の音から、音符を読み取ります。",
    stage: "transcribe",
  },
  {
    id: "quantize",
    title: "リズム補正",
    description: "読み取った音符の長さ・タイミングを、演奏可能な拍のグリッドに合わせて整えます。",
    stage: "quantize",
  },
  {
    id: "refine",
    title: "AIで整える",
    description: "小節ごとに、声部の割り当てや異名同音の表記をAIが最適化します(任意)。",
    optional: true,
  },
  {
    id: "review",
    title: "確認・編集",
    description: "譜面とピアノロールで結果を確認し、必要な箇所を手直しします。",
  },
  {
    id: "export",
    title: "書き出し",
    description: "MusicXMLまたはMIDIとして書き出します。",
  },
] as const;

export type StepStatus = "done" | "current" | "upcoming" | "locked" | "stale" | "skipped";

export interface StepState {
  id: StepId;
  status: StepStatus;
}

/**
 * `Project.stages[stage].status`は`backend/app/services/job_service.py`の
 * ジョブステータスがそのまま入る("succeeded"/"failed"/...)。「完了」を表す
 * 文字列は"succeeded"であり"completed"ではない(旧ProjectWorkspace.tsxが
 * `=== "completed"`と誤って比較しており、L1整音の実行条件が実質的に
 * 常にfalseになっていた不具合をここで修正する)。
 */
function isStageDone(project: Project, stage: string): boolean {
  const s = project.stages[stage];
  return s?.status === "succeeded" && !s.stale;
}

function isStageStale(project: Project, stage: string): boolean {
  const s = project.stages[stage];
  return s?.status === "succeeded" && s.stale === true;
}

interface DeriveOptions {
  /** ⑤AIで整える、を実行済みか(RefineSectionの成功結果があるか)。 */
  refineDone?: boolean;
  /** ⑤をユーザーが明示的にスキップしたか(UI状態、バックエンドには保存しない)。 */
  refineSkipped?: boolean;
}

/**
 * 各ステップの状態を、直前のステップの完了状況から導出する(1本道のワーク
 * フローの核)。上流ステージが再実行されて下流が古くなった(stale)場合は、
 * 以前`ProjectWorkspace.tsx`の各所に散らばっていたガード(#29-M2レビュー
 * 指摘由来)をここに集約し、該当ステップより先へ進めないようにする。
 */
export function deriveStepStates(
  project: Project | undefined,
  options: DeriveOptions = {},
): StepState[] {
  const states: StepState[] = [];
  let blocked = false;

  for (const step of STEPS) {
    if (!project) {
      states.push({ id: step.id, status: "locked" });
      continue;
    }

    if (step.stage) {
      if (blocked) {
        states.push({ id: step.id, status: "locked" });
        continue;
      }
      if (isStageStale(project, step.stage)) {
        states.push({ id: step.id, status: "stale" });
        blocked = true;
        continue;
      }
      if (isStageDone(project, step.stage)) {
        states.push({ id: step.id, status: "done" });
        continue;
      }
      states.push({ id: step.id, status: "current" });
      blocked = true;
      continue;
    }

    if (step.id === "refine") {
      if (blocked) {
        states.push({ id: step.id, status: "locked" });
        continue;
      }
      if (options.refineDone) {
        states.push({ id: step.id, status: "done" });
        continue;
      }
      if (options.refineSkipped) {
        states.push({ id: step.id, status: "skipped" });
        continue;
      }
      states.push({ id: step.id, status: "current" });
      // 任意工程なので、以降のステップをロックしない(#UI刷新で合意した方針:
      // ⑤は「任意ステップ」、blockedは更新しない)。
      continue;
    }

    // review/export: quantizeまでが完了していれば進める(quantize自体の
    // ロック判定は上のstage分岐で既に行われている)。
    if (blocked) {
      states.push({ id: step.id, status: "locked" });
      continue;
    }
    states.push({ id: step.id, status: "upcoming" });
  }

  // review/exportの1つ目を"current"にする。ただし⑤(refine)が既に"current"
  // (未実行・未スキップ)の間は、それが唯一の「次にやること」であるべきなので
  // 二重にcurrentを立てない(refineは任意工程で後続をロックしないため、
  // 何もしなければreview/exportがupcomingのまま先へ進める状態になる)。
  if (!states.some((s) => s.status === "current")) {
    const firstUpcoming = states.find((s) => s.status === "upcoming");
    if (firstUpcoming) firstUpcoming.status = "current";
  }

  return states;
}

/** 最初に「今やるべき」ステップ(current)のIDを返す。無ければ末尾。 */
export function firstActionableStep(states: StepState[]): StepId {
  return (states.find((s) => s.status === "current") ?? states[states.length - 1]).id;
}

export function stepDef(id: StepId): StepDef {
  const def = STEPS.find((s) => s.id === id);
  if (!def) throw new Error(`unknown step: ${id}`);
  return def;
}
