import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useJobStore } from "../stores/jobStore";

interface UseStageRunnerOptions {
  /** ジョブ完了時に`queryClient.invalidateQueries`するキー(複数可)。 */
  invalidateKeys: unknown[][];
  /** 失敗時に表示する既定のエラー文(ジョブ自体のmessageがあればそちらを優先)。 */
  failureFallbackMessage: string;
  /** トースト・詳細モーダルの表示用。 */
  stage: string;
  label: string;
  /** ジョブ成功時に呼ばれる(「残りを自動実行」が次のステップへ連鎖するために使う)。 */
  onSucceeded?: () => void;
  /**
   * ジョブが失敗またはキャンセルされたときに呼ばれる(Gate2レビュー指摘:
   * 「残りを自動実行」中にジョブが失敗すると、旧実装ではonSucceededが
   * 呼ばれないため自動実行フラグが解除されず、二度と自動実行できなく
   * なっていた。呼び出し側はここでautoRunRef等を止める)。
   */
  onFailedOrCancelled?: () => void;
}

/**
 * UI刷新: 旧`ProjectWorkspace.tsx`にあった「jobIdを保持→SSEの完了/失敗を
 * 検知→関連クエリをinvalidate→jobIdをクリア」という、separate/beat/
 * transcribe/quantizeの4箇所でほぼ同一だったパターン(#20/#29-M1/M2レビュー
 * 指摘由来)を1つのフックにまとめる。ワークフローの各ステップ画面
 * (`components/workflow/steps/*.tsx`)はこのフックを1回呼ぶだけでよい。
 */
export function useStageRunner(
  runFn: () => Promise<{ job_id: string }>,
  options: UseStageRunnerOptions,
) {
  const queryClient = useQueryClient();
  const track = useJobStore((s) => s.track);
  const jobs = useJobStore((s) => s.jobs);
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const job = jobId ? jobs[jobId] : undefined;
  const isRunning = jobId !== null;

  // biome-ignore lint/correctness/useExhaustiveDependencies: optionsは呼び出し側(各ステップコンポーネント)が毎レンダー新しいオブジェクトを渡すため依存に含めない。jobId/job?.statusの変化のみで十分に発火する(この効果は「ジョブが終端状態に遷移した瞬間」だけを捉えればよく、optionsは常にその時点の最新クロージャを参照する)。
  useEffect(() => {
    if (!jobId) return;
    if (job?.status === "succeeded") {
      for (const key of options.invalidateKeys) {
        void queryClient.invalidateQueries({ queryKey: key });
      }
      setJobId(null);
      options.onSucceeded?.();
    } else if (job?.status === "failed") {
      // Gate2レビュー指摘(MIDDLE): job.messageが空文字列(dsp_main.pyがstderrを
      // 出さずに終了した場合等)だと`??`はnullish判定のため空文字列をそのまま
      // 採用してしまい、意味のある既定文言が消えて空白のエラー表示になって
      // いた。`||`にして空文字列もフォールバック対象にする。
      setError(job.message || options.failureFallbackMessage);
      setJobId(null);
      options.onFailedOrCancelled?.();
    } else if (job?.status === "cancelled") {
      // Gate2レビュー指摘(MIDDLE): cancelledを処理していなかったため、
      // JobDetailModal/トーストからキャンセルすると`jobId`が残ったままになり、
      // `isRunning`がtrueに固着してボタンが二度と押せなくなっていた。
      setJobId(null);
      options.onFailedOrCancelled?.();
    }
  }, [jobId, job?.status, queryClient]);

  async function run() {
    setError(null);
    try {
      const { job_id } = await runFn();
      track(job_id, { stage: options.stage, label: options.label });
      setJobId(job_id);
      return job_id;
    } catch (err) {
      setError((err as Error).message);
      return null;
    }
  }

  return { run, isRunning, error, setError, job };
}
