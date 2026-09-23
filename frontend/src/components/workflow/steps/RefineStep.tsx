import type { Project } from "../../../api/client";
import { DiffPanel } from "../../DiffPanel";
import { RefineSection } from "../../RefineSection";
import { StepShell } from "../StepShell";

interface RefineStepProps {
  projectId: string;
  project: Project | undefined;
  activeRefineRunId: string | null;
  onRefineComplete: (runId: string) => void;
  onDismissDiff: () => void;
  onBack: () => void;
  /** 実行済みかどうかに関わらず先へ進む(未実行なら「スキップ」扱いになる、呼び出し側で判定)。 */
  onNext: () => void;
}

/** ⑤AIで整える: 任意工程。「次へ」がそのままスキップも兼ねる(合意済みの方針)。 */
export function RefineStep({
  projectId,
  project,
  activeRefineRunId,
  onRefineComplete,
  onDismissDiff,
  onBack,
  onNext,
}: RefineStepProps) {
  const quantizeStage = project?.stages.quantize;
  // ワークフロー集約(lib/workflow.ts)と同じ判定に揃える。以前はここが
  // `=== "completed"`という、DSPジョブが実際には送らない文字列と比較して
  // いたため、量子化を終えていても常にfalse(=AIで整えるが押せない)に
  // なっていた不具合を修正している。
  const isQuantizeReady = Boolean(quantizeStage?.status === "succeeded" && !quantizeStage.stale);

  return (
    <StepShell stepId="refine" stepIndex={5} onBack={onBack} onNext={onNext}>
      {!isQuantizeReady && (
        <p className="text-sm text-amber-600 dark:text-amber-400">
          先に④リズム補正を完了してください。
        </p>
      )}
      <RefineSection
        projectId={projectId}
        isQuantizeReady={isQuantizeReady}
        onRefineComplete={onRefineComplete}
      />
      {activeRefineRunId && (
        <DiffPanel projectId={projectId} runId={activeRefineRunId} onDismiss={onDismissDiff} />
      )}
    </StepShell>
  );
}
