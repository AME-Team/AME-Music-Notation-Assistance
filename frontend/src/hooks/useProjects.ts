import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createProject,
  deleteProject,
  exportProjectArchive,
  getProject,
  importProjectArchive,
  listProjects,
} from "../api/client";

const PROJECTS_KEY = ["projects"] as const;

export function useProjects() {
  return useQuery({ queryKey: PROJECTS_KEY, queryFn: listProjects });
}

/** #29: ステージの`status`/`stale`表示用に単一プロジェクトを取得する。 */
export function useProject(projectId: string) {
  return useQuery({
    queryKey: ["project", projectId],
    queryFn: () => getProject(projectId),
  });
}

export function useCreateProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => createProject(file),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: PROJECTS_KEY }),
  });
}

export function useDeleteProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (projectId: string) => deleteProject(projectId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: PROJECTS_KEY }),
  });
}

/** #65 FR-18: プロジェクトを単一アーカイブとしてダウンロードする。 */
export function useExportProjectArchive() {
  return useMutation({
    mutationFn: ({ projectId, includeStems }: { projectId: string; includeStems: boolean }) =>
      exportProjectArchive(projectId, { includeStems }),
  });
}

/** #65 FR-18: アーカイブから新規プロジェクトを作成する。 */
export function useImportProjectArchive() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => importProjectArchive(file),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: PROJECTS_KEY }),
  });
}
