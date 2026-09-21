import type { Project } from "../api/client";
import { useDeleteProject, useProjects } from "../hooks/useProjects";

interface ProjectListProps {
  selectedId: string | null;
  onSelect: (project: Project) => void;
}

export function ProjectList({ selectedId, onSelect }: ProjectListProps) {
  const { data: projects, isLoading, error } = useProjects();
  const deleteProject = useDeleteProject();

  if (isLoading) return <p className="text-sm text-gray-500 dark:text-gray-400">読み込み中...</p>;
  if (error)
    return <p className="text-sm text-red-600 dark:text-red-400">{(error as Error).message}</p>;
  if (!projects || projects.length === 0) {
    return (
      <p className="text-sm text-gray-500 dark:text-gray-400">プロジェクトはまだありません。</p>
    );
  }

  return (
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
            <p className="text-sm font-medium text-gray-900 dark:text-gray-100">{project.name}</p>
            <p className="text-xs text-gray-500 dark:text-gray-400">
              {project.audio_format.toUpperCase()} · {new Date(project.created_at).toLocaleString()}
            </p>
          </button>
          <button
            type="button"
            onClick={() => void deleteProject.mutateAsync(project.id)}
            className="text-xs text-red-500 hover:text-red-700 dark:hover:text-red-300"
          >
            削除
          </button>
        </li>
      ))}
    </ul>
  );
}
