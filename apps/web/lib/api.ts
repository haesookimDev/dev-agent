import type { AgentEvent, Artifact, WorkItem } from "./types";

export const serverApi = process.env.KELPIE_API_URL ?? "http://localhost:8000";
export const browserApi = process.env.NEXT_PUBLIC_KELPIE_API_URL ?? "http://localhost:8000";

async function get<T>(path: string): Promise<T> {
  const response = await fetch(`${serverApi}${path}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`Kelpie API returned ${response.status}`);
  return response.json() as Promise<T>;
}

export function listWorkItems(): Promise<WorkItem[]> {
  return get("/api/work-items");
}

export function getWorkItem(id: string): Promise<WorkItem> {
  return get(`/api/work-items/${id}`);
}

export function getEvents(id: string): Promise<AgentEvent[]> {
  return get(`/api/work-items/${id}/event-log`);
}

export function getArtifacts(id: string): Promise<Artifact[]> {
  return get(`/api/work-items/${id}/artifacts`);
}
