import { authenticatedFetch, browserApi } from "./browser-api";

// Match the API's artifact byte boundary; this is not a replacement for server validation.
export const MAX_PREVIEW_BYTES = 10 * 1024 * 1024;
export const PREVIEW_TIMEOUT_MS = 15_000;
export type PreviewFailure = "authentication" | "permission" | "missing" | "unavailable" |
  "tooLarge" | "unsupported" | "invalid" | "timeout" | "network" | "server";
export class ArtifactPreviewError extends Error {
  constructor(readonly reason: PreviewFailure) { super(reason); }
}
export type ArtifactPreview = { mediaType: string; size: number } & (
  { kind: "text"; text: string } | { kind: "image"; blob: Blob }
);

export function artifactURL(workId: string, artifactId: string): string {
  return `${browserApi}/api/work-items/${encodeURIComponent(workId)}/artifacts/${encodeURIComponent(artifactId)}`;
}

export async function loadArtifactPreview(workId: string, artifactId: string, signal: AbortSignal): Promise<ArtifactPreview> {
  signal.throwIfAborted();
  const controller = new AbortController();
  const cancel = () => controller.abort(signal.reason);
  signal.addEventListener("abort", cancel, { once: true });
  let timedOut = false;
  const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, PREVIEW_TIMEOUT_MS);
  try {
    // Keep the default HTTP cache; API no-store and authorization apply to every open/retry.
    const response = await authenticatedFetch(artifactURL(workId, artifactId), { signal: controller.signal, redirect: "error" });
    const reject = (reason: PreviewFailure): never => {
      void response.body?.cancel().catch(() => {});
      throw new ArtifactPreviewError(reason);
    };
    if (!response.ok) {
      const reasons: Record<number, PreviewFailure> = { 401: "authentication", 403: "permission", 404: "missing", 410: "unavailable" };
      reject(reasons[response.status] ?? "server");
    }
    const mediaType = (response.headers.get("content-type") ?? "").split(";")[0].trim().toLowerCase();
    const isText = mediaType === "text/plain" || mediaType === "application/json";
    if (!isText && !["image/png", "image/jpeg", "image/webp"].includes(mediaType)) reject("unsupported");
    const length = response.headers.get("content-length");
    if (length && /^\d+$/.test(length) && Number(length) > MAX_PREVIEW_BYTES) reject("tooLarge");
    const reader = response.body?.getReader();
    const chunks: Uint8Array[] = [];
    let size = 0;
    let complete = false;
    try {
      if (reader) {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          size += value.byteLength;
          if (size > MAX_PREVIEW_BYTES) throw new ArtifactPreviewError("tooLarge");
          chunks.push(value);
        }
      }
      complete = true;
    } finally {
      if (!complete) void reader?.cancel().catch(() => {});
      reader?.releaseLock();
    }
    controller.signal.throwIfAborted();
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    if (!isText) return { kind: "image", mediaType, size, blob: new Blob([bytes], { type: mediaType }) };
    let text: string;
    try { text = new TextDecoder("utf-8", { fatal: true }).decode(bytes); }
    catch { throw new ArtifactPreviewError("invalid"); }
    return { kind: "text", mediaType, size, text };
  } catch (error) {
    if (signal.aborted) throw signal.reason;
    if (timedOut) throw new ArtifactPreviewError("timeout");
    if (error instanceof ArtifactPreviewError) throw error;
    throw new ArtifactPreviewError("network");
  } finally {
    clearTimeout(timeout);
    signal.removeEventListener("abort", cancel);
  }
}
