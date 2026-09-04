export const browserApi = process.env.NEXT_PUBLIC_KELPIE_API_URL ?? "http://localhost:8000";

export function authenticatedFetch(input: string, init: RequestInit = {}): Promise<Response> {
  return fetch(input, { ...init, credentials: "include" });
}
