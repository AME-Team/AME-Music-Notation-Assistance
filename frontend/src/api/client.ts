import { getBackendInfo } from "../lib/backendInfo";
import type { components } from "./generated";

export type Project = components["schemas"]["Project"];
export type Job = components["schemas"]["Job"];

async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const { baseUrl, token } = await getBackendInfo();
  const headers = new Headers(init.headers);
  if (token) headers.set("X-AME-Token", token);
  const resp = await fetch(`${baseUrl}${path}`, { ...init, headers });
  if (!resp.ok) {
    const body = await resp.text().catch(() => "");
    throw new Error(`${init.method ?? "GET"} ${path} -> ${resp.status}: ${body}`);
  }
  return resp;
}

export async function listProjects(): Promise<Project[]> {
  const resp = await apiFetch("/api/projects");
  const data = (await resp.json()) as { projects: Project[] };
  return data.projects;
}

export async function createProject(file: File): Promise<Project> {
  const form = new FormData();
  form.append("file", file);
  const resp = await apiFetch("/api/projects", { method: "POST", body: form });
  return (await resp.json()) as Project;
}

export async function getProject(projectId: string): Promise<Project> {
  const resp = await apiFetch(`/api/projects/${projectId}`);
  return (await resp.json()) as Project;
}

export async function deleteProject(projectId: string): Promise<void> {
  await apiFetch(`/api/projects/${projectId}`, { method: "DELETE" });
}

export async function getOriginalAudioUrl(projectId: string): Promise<string> {
  const { baseUrl, token } = await getBackendInfo();
  // wavesurfer.js は独自に fetch するため、トークンを付けたヘッダを渡せるよう
  // fetchParams を使う側(AudioPlayer)で組み立てる。ここでは base の情報のみ返す。
  void token;
  return `${baseUrl}/api/projects/${projectId}/audio/original`;
}

export async function runDummyStage(
  projectId: string,
  params: Record<string, unknown> = {},
): Promise<{ job_id: string }> {
  const resp = await apiFetch(`/api/projects/${projectId}/stages/dummy/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ params }),
  });
  return (await resp.json()) as { job_id: string };
}

export async function getJob(jobId: string): Promise<Job> {
  const resp = await apiFetch(`/api/jobs/${jobId}`);
  return (await resp.json()) as Job;
}

export async function cancelJob(jobId: string): Promise<void> {
  await apiFetch(`/api/jobs/${jobId}/cancel`, { method: "POST" });
}
