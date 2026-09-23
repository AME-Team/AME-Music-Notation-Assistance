import { useState } from "react";
import { AgentConsole } from "../../AgentConsole";
import { AgentTaskLauncher } from "../../AgentTaskLauncher";
import { DiffPanel } from "../../DiffPanel";
import { PianoRollEditor } from "../../PianoRollEditor";
import { TrackList } from "../../TrackList";
import { TransportBar } from "../../TransportBar";
import { StepShell } from "../StepShell";

interface ReviewStepProps {
  projectId: string;
  activeAgentRunId: string | null;
  onAgentRunStarted: (runId: string) => void;
  onDismissAgent: () => void;
  onBack: () => void;
  onNext: () => void;
}

/** ⑥確認・編集: 譜面とピアノロールでの確認・手直しと、AIへの個別修正依頼(旧L2)。 */
export function ReviewStep({
  projectId,
  activeAgentRunId,
  onAgentRunStarted,
  onDismissAgent,
  onBack,
  onNext,
}: ReviewStepProps) {
  const [showAgentPanel, setShowAgentPanel] = useState(false);

  return (
    <StepShell stepId="review" stepIndex={6} onBack={onBack} onNext={onNext}>
      <PianoRollEditor key={`editor-${projectId}`} projectId={projectId} />

      <section className="space-y-2 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
        <h3 className="text-lg font-semibold text-gray-700 dark:text-gray-200">トラック</h3>
        <TrackList key={`tracks-${projectId}`} projectId={projectId} />
      </section>

      <TransportBar key={`transport-${projectId}`} projectId={projectId} />

      <details
        open={showAgentPanel}
        onToggle={(e) => setShowAgentPanel(e.currentTarget.open)}
        className="rounded-lg border border-gray-200 dark:border-gray-700 p-4"
      >
        <summary className="cursor-pointer select-none text-sm font-medium text-gray-700 dark:text-gray-200">
          AIに修正を頼む
        </summary>
        <div className="mt-4 space-y-4">
          <AgentTaskLauncher projectId={projectId} onRunStarted={onAgentRunStarted} />
          {activeAgentRunId && <AgentConsole runId={activeAgentRunId} onDismiss={onDismissAgent} />}
          {activeAgentRunId && (
            <DiffPanel
              projectId={projectId}
              runId={activeAgentRunId}
              source="agent"
              onDismiss={onDismissAgent}
            />
          )}
        </div>
      </details>
    </StepShell>
  );
}
