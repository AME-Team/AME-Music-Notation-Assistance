import type { Beatmap, Project } from "../../../api/client";
import type { useStageRunner } from "../../../hooks/useStageRunner";
import { BeatGridEditor } from "../../BeatGridEditor";
import { BeatGridOverlay } from "../../BeatGridOverlay";
import { Waveform } from "../../Waveform";
import { StepShell } from "../StepShell";

interface BeatStepProps {
  projectId: string;
  project: Project | undefined;
  peaks: { peaks: number[][]; duration_sec: number } | undefined;
  peaksLoading: boolean;
  peaksError: Error | null;
  beatmap: Beatmap | null | undefined;
  beatmapError: Error | null;
  runner: ReturnType<typeof useStageRunner>;
  onBack: () => void;
  onNext: () => void;
}

/** ②テンポ・拍の検出: 以降のすべての工程が拍の位置を基準にするため、最初に必要。 */
export function BeatStep({
  projectId,
  project,
  peaks,
  peaksLoading,
  peaksError,
  beatmap,
  beatmapError,
  runner,
  onBack,
  onNext,
}: BeatStepProps) {
  const stage = project?.stages.beat;
  const isDone = stage?.status === "succeeded" && !stage.stale;

  return (
    <StepShell
      stepId="beat"
      stepIndex={2}
      onBack={onBack}
      onNext={isDone ? onNext : undefined}
      nextDisabledReason={isDone ? undefined : "テンポ・拍の検出を完了すると次に進めます"}
    >
      <button
        type="button"
        onClick={() => void runner.run()}
        disabled={runner.isRunning}
        className="rounded-md bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 disabled:opacity-50"
      >
        {runner.isRunning ? "検出を実行中..." : isDone ? "やり直す" : "検出を開始する"}
      </button>
      {runner.error && <p className="text-sm text-red-600 dark:text-red-400">{runner.error}</p>}

      {peaksLoading && (
        <p className="text-sm text-gray-500 dark:text-gray-400">波形を読み込み中...</p>
      )}
      {peaksError && <p className="text-sm text-red-600 dark:text-red-400">{peaksError.message}</p>}
      {beatmapError && (
        <p className="text-sm text-red-600 dark:text-red-400">{beatmapError.message}</p>
      )}
      {peaks && (
        <div className="relative">
          <Waveform peaks={peaks.peaks} />
          {beatmap && <BeatGridOverlay beatmap={beatmap} durationSec={peaks.duration_sec} />}
        </div>
      )}
      {beatmap && <BeatGridEditor projectId={projectId} beatmap={beatmap} />}
    </StepShell>
  );
}
