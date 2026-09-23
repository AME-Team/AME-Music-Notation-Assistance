import { useEffect, useState } from "react";
import type { Project } from "./api/client";
import { runDummyStage } from "./api/client";
import { JobMonitor } from "./components/JobMonitor";
import { ProjectList } from "./components/ProjectList";
import { ProjectUpload } from "./components/ProjectUpload";
import { ProjectWorkspace } from "./components/ProjectWorkspace";
import { SettingsModal } from "./components/SettingsModal";
import { watchBackendStatus } from "./lib/backendStatus";
import type { BackendStatus } from "./lib/electron-api";
import { watchOpenSettings } from "./lib/settingsMenu";
import { useJobStore } from "./stores/jobStore";

export function App() {
  const [backendStatus, setBackendStatus] = useState<BackendStatus>(
    window.api ? "starting" : "ready",
  );
  const [backendDetail, setBackendDetail] = useState<string | undefined>();
  const [selected, setSelected] = useState<Project | null>(null);
  // #158: 設定はメインウィンドウに常設せず、メニュー「編集 > 設定」から開く。
  const [settingsOpen, setSettingsOpen] = useState(false);
  const track = useJobStore((s) => s.track);

  useEffect(() => {
    const api = window.api;
    if (!api) return;
    // #146: 購読に加えて現在値も取得する(mainが状態遷移を一度しか送らないため、
    // 購読前に`ready`が送られていると「起動しています」のまま固まってしまう)。
    return watchBackendStatus(api, ({ status, detail }) => {
      setBackendStatus(status);
      setBackendDetail(detail);
    });
  }, []);

  useEffect(() => watchOpenSettings(window.api, () => setSettingsOpen(true)), []);

  // UI刷新: 「ダミージョブを実行(M0動作確認用)」ボタンは開発時の動作確認用で、
  // 一般利用者には意味が伝わらず画面を占有するだけだったため撤去した。
  // e2e(`e2e/app.spec.ts`)は本フックからSSE疎通を確認する。
  useEffect(() => {
    window.__ameTestHooks = {
      runDummyJob: async (projectId: string) => {
        const { job_id } = await runDummyStage(projectId, {});
        track(job_id, { stage: "dummy", label: "動作確認" });
        return job_id;
      },
    };
    return () => {
      window.__ameTestHooks = undefined;
    };
  }, [track]);

  if (backendStatus === "starting") {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="text-gray-500 dark:text-gray-400">バックエンドを起動しています...</p>
      </div>
    );
  }
  if (backendStatus === "error") {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="text-red-600 dark:text-red-400">
          バックエンドの起動に失敗しました: {backendDetail}
        </p>
      </div>
    );
  }

  return (
    <div className="min-h-full w-full space-y-4 bg-white dark:bg-gray-950 px-4 py-4 text-gray-900 dark:text-gray-100">
      {/* UI刷新: プロジェクト一覧とワークスペース(7ステップの作業画面)を
          常に縦積みで両方表示していると、選択直後から画面が全体的に
          ゴチャゴチャして見える原因になっていた。プロジェクトを選んだら
          一覧は隠し、ワークスペースだけに集中できるようにする。 */}
      {selected ? (
        <div className="space-y-4">
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => setSelected(null)}
              className="rounded-md px-2 py-1 text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-gray-400"
            >
              ← プロジェクト一覧
            </button>
            <h1 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
              {selected.name}
            </h1>
          </div>
          <ProjectWorkspace key={selected.id} projectId={selected.id} />
        </div>
      ) : (
        <>
          <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-100">
            AME Music Notation Assistance
          </h1>
          <ProjectUpload />
          <ProjectList selectedId={null} onSelect={setSelected} />
        </>
      )}
      <JobMonitor />
      <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </div>
  );
}
