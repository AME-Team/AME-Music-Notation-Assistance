import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import type { ExecutionProvider, SeparationPreset } from "../api/client";
import { runBeatStage, runSeparateStage } from "../api/client";
import { useBeatmap } from "../hooks/useBeatmap";
import { usePeaks } from "../hooks/usePeaks";
import { useJobStore } from "../stores/jobStore";
import { AudioPlayer } from "./AudioPlayer";
import { BeatGridEditor } from "./BeatGridEditor";
import { BeatGridOverlay } from "./BeatGridOverlay";
import { TrackList } from "./TrackList";
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
  const [stageError, setStageError] = useState<string | null>(null);

  const {
    data: peaks,
    isLoading: peaksLoading,
    error: peaksError,
  } = usePeaks(projectId, "original");
  const { data: beatmap, error: beatmapError } = useBeatmap(projectId);

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
      setBeatJobId(null);
    } else if (job?.status === "failed") {
      setStageError(job.message ?? "ビート推定に失敗しました。");
      setBeatJobId(null);
    }
  }, [beatJobId, jobs, queryClient, projectId]);

  // 実行中は再押下できないようボタンを無効化する(#21-M1レビュー指摘): この
  // コンポーネントは実行中ジョブのIDを1つしか保持しないため、完了前に再度
  // 実行すると古いジョブのIDを上書きしてしまい、その完了(succeeded/failed)を
  // 検知できなくなる(例: 最初の分離が成功してステムが生成されたのに、
  // 監視対象IDが新しいジョブに差し替わっていて`stems`クエリが無効化されない)。
  // 上のuseEffectがsucceeded/failedの両方でIDをnullへ戻すため、非nullは
  // 「実行中」と同義になる。
  const isSeparateRunning = separateJobId !== null;
  const isBeatRunning = beatJobId !== null;

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

      <section className="space-y-2 rounded-lg border border-gray-200 p-4">
        <h3 className="text-lg font-semibold text-gray-700">トラック</h3>
        <TrackList key={projectId} projectId={projectId} />
      </section>
    </div>
  );
}
