import { cookies } from "next/headers";
import { notFound, redirect } from "next/navigation";
import type { AgentEvent, Artifact, WorkItem } from "./types";

const serverApi = process.env.KELPIE_API_URL ?? "http://localhost:8000";
const publicApi = process.env.NEXT_PUBLIC_KELPIE_API_URL ?? "http://localhost:8000";

async function get<T>(path: string, returnTo: string): Promise<T> {
  const cookieHeader = (await cookies()).toString();
  const response = await fetch(`${serverApi}${path}`, {
    cache: "no-store",
    headers: cookieHeader ? { cookie: cookieHeader } : undefined,
  });
  if (response.status === 401) {
    redirect(`${publicApi}/auth/login?return_to=${encodeURIComponent(returnTo)}`);
  }
  if (response.status === 404) notFound();
  if (!response.ok) throw new Error(`Kelpie API returned ${response.status}`);
  return response.json() as Promise<T>;
}

export function listWorkItems(returnTo: string): Promise<WorkItem[]> {
  return get("/api/work-items?limit=200", returnTo);
}

export function getWorkItem(id: string, returnTo: string): Promise<WorkItem> {
  return get(`/api/work-items/${id}`, returnTo);
}

export function getEvents(id: string, returnTo: string): Promise<AgentEvent[]> {
  return get(`/api/work-items/${id}/event-log`, returnTo);
}

export function getArtifacts(id: string, returnTo: string): Promise<Artifact[]> {
  return get(`/api/work-items/${id}/artifacts`, returnTo);
}
