import { useEffect, useState } from "react";
import type { Project } from "./api/client";
import { runDummyStage } from "./api/client";
import { AudioPlayer } from "./components/AudioPlayer";
import { JobMonitor } from "./components/JobMonitor";
import { ProjectList } from "./components/ProjectList";
import { ProjectUpload } from "./components/ProjectUpload";
import type { BackendStatus } from "./lib/electron-api";
import { useJobStore } from "./stores/jobStore";

export function App() {
  const [backendStatus, setBackendStatus] = useState<BackendStatus>(
    window.api ? "starting" : "ready",
  );
  const [backendDetail, setBackendDetail] = useState<string | undefined>();
  const [selected, setSelected] = useState<Project | null>(null);
  const track = useJobStore((s) => s.track);

  useEffect(() => {
    if (!window.api) return;
    return window.api.onBackendStatus((status, detail) => {
      setBackendStatus(status);
      setBackendDetail(detail);
    });
  }, []);

  if (backendStatus === "starting") {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="text-gray-500">バックエンドを起動しています...</p>
      </div>
    );
  }
  if (backendStatus === "error") {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="text-red-600">バックエンドの起動に失敗しました: {backendDetail}</p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6 p-6">
      <h1 className="text-xl font-semibold text-gray-900">AME Music Notation Assistance</h1>
      <ProjectUpload />
      <ProjectList selectedId={selected?.id ?? null} onSelect={setSelected} />
      {selected && (
        <div className="space-y-3">
          <AudioPlayer projectId={selected.id} />
          <button
            type="button"
            onClick={async () => {
              const { job_id } = await runDummyStage(selected.id, {});
              track(job_id);
            }}
            className="rounded bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-700"
          >
            ダミージョブを実行(M0動作確認用)
          </button>
        </div>
      )}
      <JobMonitor />
    </div>
  );
}
