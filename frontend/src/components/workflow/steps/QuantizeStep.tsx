import type { Project } from "../../../api/client";
import type { useStageRunner } from "../../../hooks/useStageRunner";
import { Term } from "../../ui/Term";
import { StepShell } from "../StepShell";

interface QuantizeStepProps {
  project: Project | undefined;
  runner: ReturnType<typeof useStageRunner>;
  onBack: () => void;
  onNext: () => void;
}

/** ④リズム補正(量子化): 音符の長さ・タイミングを拍のグリッドに合わせる。 */
export function QuantizeStep({ project, runner, onBack, onNext }: QuantizeStepProps) {
  const stage = project?.stages.quantize;
  const isDone = stage?.status === "succeeded" && !stage.stale;
  const transcribeStage = project?.stages.transcribe;
  const transcribeDone = transcribeStage?.status === "succeeded" && !transcribeStage.stale;

  return (
    <StepShell
      stepId="quantize"
      stepIndex={4}
      onBack={onBack}
      onNext={isDone ? onNext : undefined}
      nextDisabledReason={isDone ? undefined : "リズム補正を完了すると次に進めます"}
    >
      {!transcribeDone && (
        <p className="text-sm text-amber-600 dark:text-amber-400">
          先に③採譜を完了してください(結果が古い場合は採譜をやり直してください)。
        </p>
      )}
      <button
        type="button"
        onClick={() => void runner.run()}
        disabled={runner.isRunning || !transcribeDone}
        className="rounded-md bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 disabled:opacity-50"
      >
        {runner.isRunning ? "補正を実行中..." : isDone ? "やり直す" : "補正を開始する"}
      </button>
      {runner.error && <p className="text-sm text-red-600 dark:text-red-400">{runner.error}</p>}
      {isDone && (
        <p className="text-sm text-emerald-700 dark:text-emerald-400">
          ✓ <Term k="quantize">リズム補正</Term>が完了しました
        </p>
      )}
    </StepShell>
  );
}
