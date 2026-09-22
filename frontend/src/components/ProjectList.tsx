import { useRef, useState } from "react";
import type { Project } from "../api/client";
import {
  useDeleteProject,
  useExportProjectArchive,
  useImportProjectArchive,
  useProjects,
} from "../hooks/useProjects";

interface ProjectListProps {
  selectedId: string | null;
  onSelect: (project: Project) => void;
}

/** #65 FR-18: プロジェクトの単一アーカイブ書き出し/読み込み(ステム込みの選択・インポート)。 */
function ProjectArchiveImport() {
  const importArchive = useImportProjectArchive();
  const inputRef = useRef<HTMLInputElement>(null);

  return (
    <div className="flex items-center gap-2">
      <button
        type="button"
        onClick={() => inputRef.current?.click()}
        disabled={importArchive.isPending}
        className="rounded border border-gray-300 dark:border-gray-600 px-3 py-1.5 text-xs font-medium text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50"
      >
        {importArchive.isPending ? "読み込み中..." : "アーカイブを読み込む"}
      </button>
      <input
        ref={inputRef}
        type="file"
        aria-label="アーカイブファイルを選択"
        accept=".ameproj"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) void importArchive.mutateAsync(file);
          e.target.value = "";
        }}
      />
      {importArchive.isError && (
        <p className="text-xs text-red-600 dark:text-red-400">
          {(importArchive.error as Error).message}
        </p>
      )}
    </div>
  );
}

export function ProjectList({ selectedId, onSelect }: ProjectListProps) {
  const { data: projects, isLoading, error } = useProjects();
  const deleteProject = useDeleteProject();
  const exportArchive = useExportProjectArchive();
  // #65: 書き出すたびにステム込み/除外を選び直すのは煩雑なため、一覧全体で
  // 共有する既定値として1箇所だけ持つ(既存の削除ボタン等、行ごとの操作は
  // 即時実行のパターンに合わせ、モーダル等での都度選択は導入しない)。
  const [includeStems, setIncludeStems] = useState(true);

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <label className="flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-300">
          <input
            type="checkbox"
            checked={includeStems}
            onChange={(e) => setIncludeStems(e.target.checked)}
          />
          エクスポートにステムを含める
        </label>
        <ProjectArchiveImport />
      </div>
      {isLoading && <p className="text-sm text-gray-500 dark:text-gray-400">読み込み中...</p>}
      {error && (
        <p className="text-sm text-red-600 dark:text-red-400">{(error as Error).message}</p>
      )}
      {!isLoading && !error && (!projects || projects.length === 0) && (
        <p className="text-sm text-gray-500 dark:text-gray-400">プロジェクトはまだありません。</p>
      )}
      {projects && projects.length > 0 && (
        <ul className="divide-y divide-gray-200 dark:divide-gray-700 rounded border border-gray-200 dark:border-gray-700">
          {projects.map((project) => (
            <li
              key={project.id}
              className={`flex items-center justify-between px-4 py-3 ${
                project.id === selectedId
                  ? "bg-blue-50 dark:bg-blue-950"
                  : "hover:bg-gray-50 dark:hover:bg-gray-900"
              }`}
            >
              <button type="button" onClick={() => onSelect(project)} className="flex-1 text-left">
                <p className="text-sm font-medium text-gray-900 dark:text-gray-100">
                  {project.name}
                </p>
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  {project.audio_format.toUpperCase()} ·{" "}
                  {new Date(project.created_at).toLocaleString()}
                </p>
              </button>
              <div className="flex items-center gap-3">
                <button
                  type="button"
                  onClick={() =>
                    void exportArchive.mutateAsync({ projectId: project.id, includeStems })
                  }
                  disabled={exportArchive.isPending}
                  className="text-xs text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-300 disabled:opacity-50"
                >
                  エクスポート
                </button>
                <button
                  type="button"
                  onClick={() => void deleteProject.mutateAsync(project.id)}
                  className="text-xs text-red-500 hover:text-red-700 dark:hover:text-red-300"
                >
                  削除
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {exportArchive.isError && (
        <p className="text-sm text-red-600 dark:text-red-400">
          {(exportArchive.error as Error).message}
        </p>
      )}
    </div>
  );
}
