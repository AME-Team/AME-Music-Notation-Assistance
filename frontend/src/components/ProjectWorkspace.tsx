import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import type { ExecutionProvider, SeparationPreset } from "../api/client";
import {
  exportMusicXml,
  runBeatStage,
  runQuantizeStage,
  runSeparateStage,
  runTranscribeStage,
} from "../api/client";
import { useBeatmap } from "../hooks/useBeatmap";
import { usePeaks } from "../hooks/usePeaks";
import { useProject } from "../hooks/useProjects";
import { useJobStore } from "../stores/jobStore";
import { AudioPlayer } from "./AudioPlayer";
import { BeatGridEditor } from "./BeatGridEditor";
import { BeatGridOverlay } from "./BeatGridOverlay";
import { PianoRollEditor } from "./PianoRollEditor";
import { TrackList } from "./TrackList";
import { TransportBar } from "./TransportBar";
import { Waveform } from "./Waveform";

const PRESET_LABEL: Record<SeparationPreset, string> = {
  fast: "高速(4ステム)",
  standard: "標準(6ステム)",
  high_quality: "高品質(4ステム・低速)",
};

const EXECUTION_PROVIDER_LABEL: Record<ExecutionProvider, string> = {
  auto: "自動",
  cpu: "CPU",
  directml: "DirectML",
};

interface ProjectWorkspaceProps {
  projectId: string;
}

/** M1: 分離・ビート推定ステージの実行と、その結果(ステム/beatmap)の閲覧・補正をまとめる。 */
export function ProjectWorkspace({ projectId }: ProjectWorkspaceProps) {
  const queryClient = useQueryClient();
  const track = useJobStore((s) => s.track);
  const jobs = useJobStore((s) => s.jobs);

  const [preset, setPreset] = useState<SeparationPreset>("standard");
  const [executionProvider, setExecutionProvider] = useState<ExecutionProvider>("auto");
  const [separateJobId, setSeparateJobId] = useState<string | null>(null);
  const [beatJobId, setBeatJobId] = useState<string | null>(null);
  const [transcribeJobId, setTranscribeJobId] = useState<string | null>(null);
  const [quantizeJobId, setQuantizeJobId] = useState<string | null>(null);
  const [stageError, setStageError] = useState<string | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const [isExporting, setIsExporting] = useState(false);

  const {
    data: peaks,
    isLoading: peaksLoading,
    error: peaksError,
  } = usePeaks(projectId, "original");
  const { data: beatmap, error: beatmapError } = useBeatmap(projectId);
  const { data: project } = useProject(projectId);
  const transcribeStage = project?.stages.transcribe;
  const quantizeStage = project?.stages.quantize;

  // ジョブが完了したら、その成果物に依存するクエリを再取得する(#16/#18)。
  // #13のJobMonitor/jobStoreはSSEで進捗を追うだけでキャッシュ無効化までは
  // 行わないため、ここで監視して繋ぎ込む。失敗(failed)時もIDを残したままに
  // すると再実行するまで永久に反応しなくなるため、成功と同様に検知して
  // クリアし、エラーメッセージを表示する(#20-M1レビュー指摘)。
  useEffect(() => {
    if (!separateJobId) return;
    const job = jobs[separateJobId];
    if (job?.status === "succeeded") {
      void queryClient.invalidateQueries({ queryKey: ["stems", projectId] });
      void queryClient.invalidateQueries({ queryKey: ["project", projectId] });
      setSeparateJobId(null);
    } else if (job?.status === "failed") {
      setStageError(job.message ?? "音源分離に失敗しました。");
      setSeparateJobId(null);
    }
  }, [separateJobId, jobs, queryClient, projectId]);

  useEffect(() => {
    if (!beatJobId) return;
    const job = jobs[beatJobId];
    if (job?.status === "succeeded") {
      void queryClient.invalidateQueries({ queryKey: ["beatmap", projectId] });
      void queryClient.invalidateQueries({ queryKey: ["peaks", projectId, "original"] });
      void queryClient.invalidateQueries({ queryKey: ["project", projectId] });
      setBeatJobId(null);
    } else if (job?.status === "failed") {
      setStageError(job.message ?? "ビート推定に失敗しました。");
      setBeatJobId(null);
    }
  }, [beatJobId, jobs, queryClient, projectId]);

  // #29: transcribe/quantizeもseparate/beatと同じ「実行→完了検知→関連クエリの
  // 無効化」パターンに揃える。両ステージともScore IR自体を直接表示するUIは
  // M2スコープ外(ユーザー決定済み)のため、`project`(stages.status/stale)のみ
  // 無効化すれば十分。
  useEffect(() => {
    if (!transcribeJobId) return;
    const job = jobs[transcribeJobId];
    if (job?.status === "succeeded") {
      void queryClient.invalidateQueries({ queryKey: ["project", projectId] });
      setTranscribeJobId(null);
    } else if (job?.status === "failed") {
      setStageError(job.message ?? "採譜に失敗しました。");
      setTranscribeJobId(null);
    }
  }, [transcribeJobId, jobs, queryClient, projectId]);

  useEffect(() => {
    if (!quantizeJobId) return;
    const job = jobs[quantizeJobId];
    if (job?.status === "succeeded") {
      void queryClient.invalidateQueries({ queryKey: ["project", projectId] });
      setQuantizeJobId(null);
    } else if (job?.status === "failed") {
      setStageError(job.message ?? "量子化に失敗しました。");
      setQuantizeJobId(null);
    }
  }, [quantizeJobId, jobs, queryClient, projectId]);

  // 実行中は再押下できないようボタンを無効化する(#21-M1レビュー指摘): この
  // コンポーネントは実行中ジョブのIDを1つしか保持しないため、完了前に再度
  // 実行すると古いジョブのIDを上書きしてしまい、その完了(succeeded/failed)を
  // 検知できなくなる(例: 最初の分離が成功してステムが生成されたのに、
  // 監視対象IDが新しいジョブに差し替わっていて`stems`クエリが無効化されない)。
  // 上のuseEffectがsucceeded/failedの両方でIDをnullへ戻すため、非nullは
  // 「実行中」と同義になる。
  const isSeparateRunning = separateJobId !== null;
  const isBeatRunning = beatJobId !== null;
  const isTranscribeRunning = transcribeJobId !== null;
  const isQuantizeRunning = quantizeJobId !== null;

  async function handleRunSeparate() {
    setStageError(null);
    try {
      const { job_id } = await runSeparateStage(projectId, preset, executionProvider);
      track(job_id);
      setSeparateJobId(job_id);
    } catch (err) {
      // runSeparateStage自体の呼び出し(HTTPリクエスト)が失敗した場合
      // (ジョブ登録前のエラー、例: 422/500)。ジョブ開始後の失敗は上のuseEffectで
      // status==="failed"として検知する(#20-M1レビュー指摘)。
      setStageError((err as Error).message);
    }
  }

  async function handleRunBeat() {
    setStageError(null);
    try {
      const { job_id } = await runBeatStage(projectId);
      track(job_id);
      setBeatJobId(job_id);
    } catch (err) {
      setStageError((err as Error).message);
    }
  }

  async function handleRunTranscribe() {
    setStageError(null);
    try {
      const { job_id } = await runTranscribeStage(projectId);
      track(job_id);
      setTranscribeJobId(job_id);
    } catch (err) {
      setStageError((err as Error).message);
    }
  }

  async function handleRunQuantize() {
    setStageError(null);
    try {
      const { job_id } = await runQuantizeStage(projectId);
      track(job_id);
      setQuantizeJobId(job_id);
    } catch (err) {
      setStageError((err as Error).message);
    }
  }

  async function handleExport() {
    setExportError(null);
    setIsExporting(true);
    try {
      await exportMusicXml(projectId);
    } catch (err) {
      setExportError((err as Error).message);
    } finally {
      setIsExporting(false);
    }
  }

  return (
    <div className="space-y-6">
      <AudioPlayer projectId={projectId} />

      <section className="space-y-3 rounded-lg border border-gray-200 p-4">
        <h3 className="text-lg font-semibold text-gray-700">分離・ビート推定</h3>
        <div className="flex flex-wrap items-end gap-4">
          <label className="flex flex-col gap-1 text-sm text-gray-600">
            プリセット
            <select
              value={preset}
              onChange={(event) => setPreset(event.target.value as SeparationPreset)}
              className="rounded-md border border-gray-300 px-2 py-1 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
            >
              {(Object.keys(PRESET_LABEL) as SeparationPreset[]).map((value) => (
                <option key={value} value={value}>
                  {PRESET_LABEL[value]}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm text-gray-600">
            実行プロバイダ
            <select
              value={executionProvider}
              onChange={(event) => setExecutionProvider(event.target.value as ExecutionProvider)}
              className="rounded-md border border-gray-300 px-2 py-1 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
            >
              {(Object.keys(EXECUTION_PROVIDER_LABEL) as ExecutionProvider[]).map((value) => (
                <option key={value} value={value}>
                  {EXECUTION_PROVIDER_LABEL[value]}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            onClick={() => void handleRunSeparate()}
            disabled={isSeparateRunning}
            className="rounded-md bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 disabled:opacity-50"
          >
            {isSeparateRunning ? "音源分離を実行中..." : "音源分離を実行"}
          </button>
          <button
            type="button"
            onClick={() => void handleRunBeat()}
            disabled={isBeatRunning}
            className="rounded-md bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 disabled:opacity-50"
          >
            {isBeatRunning ? "ビート推定を実行中..." : "ビート推定を実行"}
          </button>
        </div>
        {stageError && <p className="text-sm text-red-600">{stageError}</p>}
      </section>

      <section className="space-y-3 rounded-lg border border-gray-200 p-4">
        <h3 className="text-lg font-semibold text-gray-700">採譜・量子化・エクスポート</h3>
        <div className="flex flex-wrap items-center gap-4">
          <button
            type="button"
            onClick={() => void handleRunTranscribe()}
            disabled={isTranscribeRunning}
            className="rounded-md bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 disabled:opacity-50"
          >
            {isTranscribeRunning ? "採譜を実行中..." : "採譜を実行"}
          </button>
          {/* #29-M2レビュー指摘: separateを再実行するとtranscribeとquantizeの
              両方が無効化される。transcribeのstaleを表示せずquantizeボタンを
              押せてしまうと、無効化済みの古いscore/current.jsonをそのまま
              量子化してしまい、quantize自身のmetaが書かれてstale=Falseに
              戻るため、transcribeが古いことがUIから見えなくなる(誤った
              MusicXMLをエクスポートしうる)。transcribeがstaleの間は
              量子化ボタン自体を無効化する。 */}
          {transcribeStage?.stale && (
            <span className="text-sm text-amber-600">
              採譜結果が古い可能性があります(再実行してください)
            </span>
          )}
          <button
            type="button"
            onClick={() => void handleRunQuantize()}
            disabled={isQuantizeRunning || transcribeStage?.stale}
            className="rounded-md bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 disabled:opacity-50"
          >
            {isQuantizeRunning ? "量子化を実行中..." : "量子化を実行"}
          </button>
          {quantizeStage?.stale && (
            <span className="text-sm text-amber-600">
              量子化結果が古い可能性があります(再実行してください)
            </span>
          )}
          <button
            type="button"
            onClick={() => void handleExport()}
            // #29-M2レビュー指摘: 量子化ボタンをtranscribe staleで無効化した
            // 意図(古いScore IRからの誤ったMusicXML出力を防ぐ)と揃え、
            // エクスポート側でも同じガードを掛ける(quantize/transcribeの
            // どちらかがstaleなら、古いonset_tick等のままの出力になりうる)。
            disabled={isExporting || quantizeStage?.stale || transcribeStage?.stale}
            className="rounded-md bg-emerald-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-emerald-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-emerald-500 disabled:opacity-50"
          >
            {isExporting ? "エクスポート中..." : "MusicXMLをエクスポート"}
          </button>
        </div>
        {exportError && <p className="text-sm text-red-600">{exportError}</p>}
      </section>

      <section className="space-y-2 rounded-lg border border-gray-200 p-4">
        <h3 className="text-lg font-semibold text-gray-700">波形とビートグリッド</h3>
        {peaksLoading && <p className="text-sm text-gray-500">波形を読み込み中...</p>}
        {peaksError && <p className="text-sm text-red-600">{(peaksError as Error).message}</p>}
        {beatmapError && (
          // beatmapが未実行(404)ならuseBeatmap/getBeatmapがnullを返すため、
          // ここに表示されるのはそれ以外の想定外エラー(#21-M1レビュー指摘:
          // peaksと異なりbeatmapの取得エラーだけ無表示になっていた)。
          <p className="text-sm text-red-600">{(beatmapError as Error).message}</p>
        )}
        {peaks && (
          <div className="relative">
            <Waveform peaks={peaks.peaks} />
            {beatmap && <BeatGridOverlay beatmap={beatmap} durationSec={peaks.duration_sec} />}
          </div>
        )}
      </section>

      {beatmap && <BeatGridEditor projectId={projectId} beatmap={beatmap} />}

      <PianoRollEditor key={projectId} projectId={projectId} />

      <section className="space-y-2 rounded-lg border border-gray-200 p-4">
        <h3 className="text-lg font-semibold text-gray-700">トラック</h3>
        <TrackList key={projectId} projectId={projectId} />
      </section>

      <TransportBar key={projectId} projectId={projectId} />
    </div>
  );
}
