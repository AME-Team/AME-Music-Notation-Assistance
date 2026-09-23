import { useEffect, useRef, useState } from "react";
import type { ExecutionProvider, ExportFormat, SeparationPreset } from "../api/client";
import {
  exportScore,
  runBeatStage,
  runQuantizeStage,
  runSeparateStage,
  runTranscribeStage,
} from "../api/client";
import { useBeatmap } from "../hooks/useBeatmap";
import { usePeaks } from "../hooks/usePeaks";
import { useProject } from "../hooks/useProjects";
import { useStageRunner } from "../hooks/useStageRunner";
import { deriveStepStates, firstActionableStep, STEPS, type StepId } from "../lib/workflow";
import { StepSidebar } from "./workflow/StepSidebar";
import { BeatStep } from "./workflow/steps/BeatStep";
import { ExportStep } from "./workflow/steps/ExportStep";
import { QuantizeStep } from "./workflow/steps/QuantizeStep";
import { RefineStep } from "./workflow/steps/RefineStep";
import { ReviewStep } from "./workflow/steps/ReviewStep";
import { SeparateStep } from "./workflow/steps/SeparateStep";
import { TranscribeStep } from "./workflow/steps/TranscribeStep";

interface ProjectWorkspaceProps {
  projectId: string;
}

type DspStageId = "separate" | "beat" | "transcribe" | "quantize";
const DSP_STAGE_ORDER: DspStageId[] = ["separate", "beat", "transcribe", "quantize"];

function lastStepStorageKey(projectId: string): string {
  return `ame:lastStep:${projectId}`;
}

/**
 * UI刷新: 画面全体を「左にステップ一覧、右に今のステップの作業」という
 * 1本道の構成にする(以前はすべての機能を縦一列に並べているだけで、作業の
 * 順番が画面から読み取れなかった)。DSPジョブ(separate/beat/transcribe/
 * quantize)の起動・監視は`useStageRunner`にまとめ、このコンポーネントは
 * それらを4回呼び出して各ステップ画面へ配るだけの薄いコンテナにする。
 */
export function ProjectWorkspace({ projectId }: ProjectWorkspaceProps) {
  const [preset, setPreset] = useState<SeparationPreset>("standard");
  const [executionProvider, setExecutionProvider] = useState<ExecutionProvider>("auto");
  const [exportError, setExportError] = useState<string | null>(null);
  const [exportingFormat, setExportingFormat] = useState<ExportFormat | null>(null);
  const [activeRefineRunId, setActiveRefineRunId] = useState<string | null>(null);
  const [refineSkipped, setRefineSkipped] = useState(false);
  const [activeAgentRunId, setActiveAgentRunId] = useState<string | null>(null);
  const [isAutoRunning, setIsAutoRunning] = useState(false);
  const autoRunRef = useRef(false);

  const {
    data: peaks,
    isLoading: peaksLoading,
    error: peaksError,
  } = usePeaks(projectId, "original");
  const { data: beatmap, error: beatmapError } = useBeatmap(projectId);
  const { data: project } = useProject(projectId);

  const [activeStep, setActiveStep] = useState<StepId>(() => {
    try {
      const saved = localStorage.getItem(lastStepStorageKey(projectId));
      return (saved as StepId) ?? "separate";
    } catch {
      return "separate";
    }
  });
  const hasAutoNavigatedRef = useRef(false);

  function goTo(step: StepId) {
    setActiveStep(step);
    try {
      localStorage.setItem(lastStepStorageKey(projectId), step);
    } catch {
      // localStorageが使えない環境(プライベートモード等)では単に永続化を諦める。
      // 画面の切り替え自体はReact stateで完結するため機能上の問題はない。
    }
  }

  // biome-ignore lint/correctness/useExhaustiveDependencies: プロジェクトを開いた直後の1回だけ最初に着手すべきステップへ自動で移動したいため、依存は意図的にproject(の到着)だけに絞る。activeStep/refineSkipped/goToをここに含めると、ユーザーが手動でステップを切り替えるたびにこの初回誘導ロジックが再評価されてしまう(hasAutoNavigatedRefで1回きりに制限している意図と矛盾する)。
  useEffect(() => {
    if (!project || hasAutoNavigatedRef.current) return;
    hasAutoNavigatedRef.current = true;
    const states = deriveStepStates(project, {
      refineDone: activeRefineRunId !== null,
      refineSkipped,
    });
    const currentState = states.find((s) => s.id === activeStep);
    if (!currentState || currentState.status === "locked") {
      goTo(firstActionableStep(states));
    }
  }, [project]);

  function advanceAutoRun(justFinished: DspStageId) {
    if (!autoRunRef.current) return;
    const idx = DSP_STAGE_ORDER.indexOf(justFinished);
    const next = DSP_STAGE_ORDER[idx + 1];
    if (!next) {
      autoRunRef.current = false;
      setIsAutoRunning(false);
      return;
    }
    goTo(next);
    void runnersRef.current[next]();
  }

  const separateRunner = useStageRunner(
    () => runSeparateStage(projectId, preset, executionProvider),
    {
      invalidateKeys: [
        ["stems", projectId],
        ["project", projectId],
      ],
      failureFallbackMessage: "音源分離に失敗しました。",
      stage: "separate",
      label: "音源分離",
      onSucceeded: () => advanceAutoRun("separate"),
    },
  );
  const beatRunner = useStageRunner(() => runBeatStage(projectId), {
    invalidateKeys: [
      ["beatmap", projectId],
      ["peaks", projectId, "original"],
      ["project", projectId],
    ],
    failureFallbackMessage: "テンポ・拍の検出に失敗しました。",
    stage: "beat",
    label: "テンポ・拍の検出",
    onSucceeded: () => advanceAutoRun("beat"),
  });
  const transcribeRunner = useStageRunner(() => runTranscribeStage(projectId), {
    invalidateKeys: [["project", projectId]],
    failureFallbackMessage: "採譜に失敗しました。",
    stage: "transcribe",
    label: "採譜",
    onSucceeded: () => advanceAutoRun("transcribe"),
  });
  const quantizeRunner = useStageRunner(() => runQuantizeStage(projectId), {
    invalidateKeys: [["project", projectId]],
    failureFallbackMessage: "リズム補正に失敗しました。",
    stage: "quantize",
    label: "リズム補正",
    onSucceeded: () => advanceAutoRun("quantize"),
  });

  const runnersRef = useRef<Record<DspStageId, () => Promise<string | null>>>({
    separate: separateRunner.run,
    beat: beatRunner.run,
    transcribe: transcribeRunner.run,
    quantize: quantizeRunner.run,
  });
  runnersRef.current = {
    separate: separateRunner.run,
    beat: beatRunner.run,
    transcribe: transcribeRunner.run,
    quantize: quantizeRunner.run,
  };

  const states = deriveStepStates(project, {
    refineDone: activeRefineRunId !== null,
    refineSkipped,
  });

  const anyDspRunning =
    separateRunner.isRunning ||
    beatRunner.isRunning ||
    transcribeRunner.isRunning ||
    quantizeRunner.isRunning;
  const nextDspStage = DSP_STAGE_ORDER.find((id) => {
    const status = states.find((s) => s.id === id)?.status;
    return status === "current" || status === "stale";
  });

  function handleRunRemaining() {
    if (!nextDspStage || anyDspRunning) return;
    autoRunRef.current = true;
    setIsAutoRunning(true);
    goTo(nextDspStage);
    void runnersRef.current[nextDspStage]();
  }

  async function handleExport(format: ExportFormat) {
    setExportError(null);
    setExportingFormat(format);
    try {
      await exportScore(projectId, format);
    } catch (err) {
      setExportError((err as Error).message);
    } finally {
      setExportingFormat(null);
    }
  }

  function handleSelectStep(id: StepId) {
    const status = states.find((s) => s.id === id)?.status;
    if (status === "locked") return;
    goTo(id);
  }

  function stepIndexOf(id: StepId): number {
    return STEPS.findIndex((s) => s.id === id);
  }
  function neighbor(offset: number): StepId | undefined {
    return STEPS[stepIndexOf(activeStep) + offset]?.id;
  }

  return (
    <div className="flex gap-4">
      <StepSidebar
        states={states}
        activeStep={activeStep}
        onSelect={handleSelectStep}
        onRunRemaining={handleRunRemaining}
        canRunRemaining={Boolean(nextDspStage) && !anyDspRunning}
        isRunningRemaining={isAutoRunning}
      />

      {activeStep === "separate" && (
        <SeparateStep
          projectId={projectId}
          project={project}
          preset={preset}
          onPresetChange={setPreset}
          executionProvider={executionProvider}
          onExecutionProviderChange={setExecutionProvider}
          runner={separateRunner}
          onNext={() => goTo(neighbor(1) ?? "separate")}
        />
      )}
      {activeStep === "beat" && (
        <BeatStep
          projectId={projectId}
          project={project}
          peaks={peaks}
          peaksLoading={peaksLoading}
          peaksError={peaksError as Error | null}
          beatmap={beatmap}
          beatmapError={beatmapError as Error | null}
          runner={beatRunner}
          onBack={() => goTo(neighbor(-1) ?? "beat")}
          onNext={() => goTo(neighbor(1) ?? "beat")}
        />
      )}
      {activeStep === "transcribe" && (
        <TranscribeStep
          project={project}
          runner={transcribeRunner}
          onBack={() => goTo(neighbor(-1) ?? "transcribe")}
          onNext={() => goTo(neighbor(1) ?? "transcribe")}
        />
      )}
      {activeStep === "quantize" && (
        <QuantizeStep
          project={project}
          runner={quantizeRunner}
          onBack={() => goTo(neighbor(-1) ?? "quantize")}
          onNext={() => goTo(neighbor(1) ?? "quantize")}
        />
      )}
      {activeStep === "refine" && (
        <RefineStep
          projectId={projectId}
          project={project}
          activeRefineRunId={activeRefineRunId}
          onRefineComplete={setActiveRefineRunId}
          onDismissDiff={() => setActiveRefineRunId(null)}
          onBack={() => goTo(neighbor(-1) ?? "refine")}
          onNext={() => {
            if (!activeRefineRunId) setRefineSkipped(true);
            goTo(neighbor(1) ?? "refine");
          }}
        />
      )}
      {activeStep === "review" && (
        <ReviewStep
          projectId={projectId}
          activeAgentRunId={activeAgentRunId}
          onAgentRunStarted={setActiveAgentRunId}
          onDismissAgent={() => setActiveAgentRunId(null)}
          onBack={() => goTo(neighbor(-1) ?? "review")}
          onNext={() => goTo(neighbor(1) ?? "review")}
        />
      )}
      {activeStep === "export" && (
        <ExportStep
          project={project}
          exportingFormat={exportingFormat}
          exportError={exportError}
          onExport={(format) => void handleExport(format)}
          onBack={() => goTo(neighbor(-1) ?? "export")}
        />
      )}
    </div>
  );
}
