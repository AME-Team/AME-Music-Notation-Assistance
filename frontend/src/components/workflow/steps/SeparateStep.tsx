import { useState } from "react";
import type { ExecutionProvider, Project, SeparationPreset } from "../../../api/client";
import type { useStageRunner } from "../../../hooks/useStageRunner";
import { AudioPlayer } from "../../AudioPlayer";
import { TrackList } from "../../TrackList";
import { Term } from "../../ui/Term";
import { StepShell } from "../StepShell";

const PRESET_LABEL: Record<SeparationPreset, string> = {
  fast: "高速(4ステム)",
  standard: "標準(6ステム・採譜に必要)",
  high_quality: "高品質(4ステム・低速)",
};

const EXECUTION_PROVIDER_LABEL: Record<ExecutionProvider, string> = {
  auto: "自動",
  cpu: "CPU",
  directml: "DirectML",
};

interface SeparateStepProps {
  projectId: string;
  project: Project | undefined;
  preset: SeparationPreset;
  onPresetChange: (preset: SeparationPreset) => void;
  executionProvider: ExecutionProvider;
  onExecutionProviderChange: (provider: ExecutionProvider) => void;
  runner: ReturnType<typeof useStageRunner>;
  onNext: () => void;
}

/** ①音源分離: 採譜に必要な`standard`プリセットを既定にする(#UI刷新)。
 * 採譜(Stage3)は`htdemucs_6s`(=`standard`)が生成するステムにしか対応
 * しておらず(`backend/app/worker/dsp_main.py`の`run_transcribe_stage`)、
 * 他のプリセットを選ぶと後で採譜が失敗する落とし穴だった。 */
export function SeparateStep({
  projectId,
  project,
  preset,
  onPresetChange,
  executionProvider,
  onExecutionProviderChange,
  runner,
  onNext,
}: SeparateStepProps) {
  const [showAdvanced, setShowAdvanced] = useState(false);
  const stage = project?.stages.separate;
  const isDone = stage?.status === "succeeded" && !stage.stale;

  return (
    <StepShell
      stepId="separate"
      stepIndex={1}
      onNext={isDone ? onNext : undefined}
      nextDisabledReason={isDone ? undefined : "音源分離を完了すると次に進めます"}
    >
      <AudioPlayer projectId={projectId} />

      <details
        open={showAdvanced}
        onToggle={(e) => setShowAdvanced(e.currentTarget.open)}
        className="rounded-md border border-gray-200 dark:border-gray-700 p-3"
      >
        <summary className="cursor-pointer select-none text-sm font-medium text-gray-700 dark:text-gray-200">
          詳細設定
        </summary>
        <div className="mt-3 flex flex-wrap items-end gap-4">
          <label className="flex flex-col gap-1 text-sm text-gray-600 dark:text-gray-300">
            <Term k="preset">プリセット</Term>
            <select
              value={preset}
              onChange={(event) => onPresetChange(event.target.value as SeparationPreset)}
              className="rounded-md border border-gray-300 dark:border-gray-600 px-2 py-1 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
            >
              {(Object.keys(PRESET_LABEL) as SeparationPreset[]).map((value) => (
                <option key={value} value={value}>
                  {PRESET_LABEL[value]}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm text-gray-600 dark:text-gray-300">
            <Term k="executionProvider">計算に使う装置</Term>
            <select
              value={executionProvider}
              onChange={(event) =>
                onExecutionProviderChange(event.target.value as ExecutionProvider)
              }
              className="rounded-md border border-gray-300 dark:border-gray-600 px-2 py-1 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
            >
              {(Object.keys(EXECUTION_PROVIDER_LABEL) as ExecutionProvider[]).map((value) => (
                <option key={value} value={value}>
                  {EXECUTION_PROVIDER_LABEL[value]}
                </option>
              ))}
            </select>
          </label>
        </div>
      </details>

      <button
        type="button"
        onClick={() => void runner.run()}
        disabled={runner.isRunning}
        className="rounded-md bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 disabled:opacity-50"
      >
        {runner.isRunning
          ? "音源分離を実行中..."
          : isDone
            ? "音源分離をやり直す"
            : "音源分離を開始する"}
      </button>
      {runner.error && <p className="text-sm text-red-600 dark:text-red-400">{runner.error}</p>}

      {isDone && (
        <div className="space-y-2">
          <p className="text-sm text-emerald-700 dark:text-emerald-400">✓ 分離が完了しました</p>
          <TrackList key={`tracks-${projectId}`} projectId={projectId} />
        </div>
      )}
    </StepShell>
  );
}
