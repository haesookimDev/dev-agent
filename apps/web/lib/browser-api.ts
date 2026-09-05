export const browserApi = process.env.NEXT_PUBLIC_KELPIE_API_URL ?? "http://localhost:8000";

export function authenticatedFetch(input: string, init: RequestInit = {}): Promise<Response> {
  return fetch(input, { ...init, credentials: "include" });
}

export class BrowserAPIError extends Error {
  constructor(readonly status: number) { super(`API returned ${status}`); }
}

export async function apiJSON<T>(input: string, init: RequestInit = {}): Promise<T> {
  const response = await authenticatedFetch(input, init);
  if (!response.ok) throw new BrowserAPIError(response.status);
  return response.json() as Promise<T>;
}

export function requestErrorMessage(error: unknown, fallback: string, network: string, permission: string): string {
  if (error instanceof BrowserAPIError) return error.status === 403 ? permission : `${fallback} (${error.status})`;
  return network;
}
