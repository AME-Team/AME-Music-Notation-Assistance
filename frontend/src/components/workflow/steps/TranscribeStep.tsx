import type { Project } from "../../../api/client";
import type { useStageRunner } from "../../../hooks/useStageRunner";
import { StepShell } from "../StepShell";

interface TranscribeStepProps {
  project: Project | undefined;
  runner: ReturnType<typeof useStageRunner>;
  onBack: () => void;
  onNext: () => void;
}

/** ③採譜: 分離した各楽器の音から音符を読み取る。時間がかかるため、詳細はJobDetailModal(トーストの[詳細])で確認する。 */
export function TranscribeStep({ project, runner, onBack, onNext }: TranscribeStepProps) {
  const stage = project?.stages.transcribe;
  const isDone = stage?.status === "succeeded" && !stage.stale;
  const separateStage = project?.stages.separate;
  const separateDone = separateStage?.status === "succeeded" && !separateStage.stale;

  return (
    <StepShell
      stepId="transcribe"
      stepIndex={3}
      onBack={onBack}
      onNext={isDone ? onNext : undefined}
      nextDisabledReason={isDone ? undefined : "採譜を完了すると次に進めます"}
    >
      {!separateDone && (
        <p className="text-sm text-amber-600 dark:text-amber-400">
          先に①音源分離(標準プリセット)を完了してください。
        </p>
      )}
      <button
        type="button"
        onClick={() => void runner.run()}
        disabled={runner.isRunning || !separateDone}
        className="rounded-md bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 disabled:opacity-50"
      >
        {runner.isRunning ? "採譜を実行中..." : isDone ? "採譜をやり直す" : "採譜を開始する"}
      </button>
      {runner.error && <p className="text-sm text-red-600 dark:text-red-400">{runner.error}</p>}
      {isDone && (
        <p className="text-sm text-emerald-700 dark:text-emerald-400">✓ 採譜が完了しました</p>
      )}
    </StepShell>
  );
}
