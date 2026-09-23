import type { ExportFormat, Project } from "../../../api/client";
import { Term } from "../../ui/Term";
import { StepShell } from "../StepShell";

interface ExportStepProps {
  project: Project | undefined;
  exportingFormat: ExportFormat | null;
  exportError: string | null;
  onExport: (format: ExportFormat) => void;
  onBack: () => void;
}

/** ⑦書き出し: MusicXML/MIDIの2形式。#36: MIDIは記譜情報を失うことを明示する。 */
export function ExportStep({
  project,
  exportingFormat,
  exportError,
  onExport,
  onBack,
}: ExportStepProps) {
  const isExporting = exportingFormat !== null;
  const quantizeStage = project?.stages.quantize;
  const transcribeStage = project?.stages.transcribe;
  const isReady = Boolean(
    quantizeStage?.status === "succeeded" &&
      !quantizeStage.stale &&
      transcribeStage?.status === "succeeded" &&
      !transcribeStage.stale,
  );

  return (
    <StepShell stepId="export" stepIndex={7} onBack={onBack}>
      {!isReady && (
        <p className="text-sm text-amber-600 dark:text-amber-400">
          先に④リズム補正までを完了(最新の状態に)してください。
        </p>
      )}
      <div className="flex flex-wrap items-center gap-4">
        <button
          type="button"
          onClick={() => onExport("musicxml")}
          disabled={isExporting || !isReady}
          className="rounded-md bg-emerald-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-emerald-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-emerald-500 disabled:opacity-50"
        >
          {exportingFormat === "musicxml" ? "エクスポート中..." : "MusicXMLをエクスポート"}
        </button>
        <button
          type="button"
          onClick={() => onExport("midi")}
          disabled={isExporting || !isReady}
          className="rounded-md bg-emerald-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-emerald-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-emerald-500 disabled:opacity-50"
        >
          {exportingFormat === "midi" ? "エクスポート中..." : "MIDIをエクスポート"}
        </button>
      </div>
      <p className="text-xs text-gray-500 dark:text-gray-400">
        ⚠ <Term k="midi">MIDI</Term>(SMF)形式では音名表記・声部・大譜表の情報は失われます(音高・
        タイミングのみ保持されます)。記譜情報を保持したい場合は<Term k="musicxml">MusicXML</Term>
        を使用してください。
      </p>
      {exportError && <p className="text-sm text-red-600 dark:text-red-400">{exportError}</p>}
    </StepShell>
  );
}
