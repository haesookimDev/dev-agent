import { afterEach, describe, expect, it, vi } from "vitest";
import { artifactURL, ArtifactPreviewError, loadArtifactPreview, MAX_PREVIEW_BYTES, PREVIEW_TIMEOUT_MS } from "./artifact-preview";

const open = (signal = new AbortController().signal) => loadArtifactPreview("work", "file", signal);
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

describe("bounded artifact previews", () => {
  it.each([
    ['{"detail":"artifact retention period has expired"}', "expired"],
    ['{"detail":"artifact content is unavailable"}', "unavailable"],
    ['{"detail":"private diagnostic"}', "unavailable"],
    ['null', "unavailable"], ['not JSON', "unavailable"],
  ])("interprets only the fixed retention reason in a bounded 410 JSON response", async (body, reason) => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(body, {
      status: 410, headers: { "content-type": "application/json" },
    }));
    await expect(open()).rejects.toEqual(new ArtifactPreviewError(reason as "expired" | "unavailable"));
  });

  it("cancels oversized 410 bodies without treating their diagnostics as expiration", async () => {
    const cancelled = vi.fn();
    const body = new ReadableStream({ start(c) { c.enqueue(new Uint8Array(1025)); }, cancel: cancelled });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(body, {
      status: 410, headers: { "content-type": "application/json", "content-length": "1" },
    }));
    await expect(open()).rejects.toEqual(new ArtifactPreviewError("unavailable"));
    expect(cancelled).toHaveBeenCalledOnce();
    expect(body.locked).toBe(false);
  });

  it.each(["text/plain", "application/json", "Text/Plain; charset=utf-8"])("reads %s as literal UTF-8 text", async (type) => {
    const text = '<script>privateMarkup()</script> 한글 ✅';
    const fetcher = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(text, { headers: { "content-type": type } }));
    await expect(open()).resolves.toMatchObject({ kind: "text", text, size: new TextEncoder().encode(text).length });
    expect(fetcher).toHaveBeenCalledWith(artifactURL("work", "file"), { credentials: "include", signal: expect.any(AbortSignal), redirect: "error" });
    expect(fetcher.mock.calls[0][1]).not.toHaveProperty("cache");
  });

  it.each(["image/png", "image/jpeg", "image/webp"])("keeps %s bytes in a typed Blob", async (type) => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(new Uint8Array([1, 2, 3]), { headers: { "content-type": type } }));
    const preview = await open();
    expect(preview.kind).toBe("image");
    if (preview.kind !== "image") throw new Error("Image required");
    expect(preview.blob.type).toBe(type);
    expect(new Uint8Array(await preview.blob.arrayBuffer())).toEqual(new Uint8Array([1, 2, 3]));
  });

  it.each([[401, "authentication"], [403, "permission"], [404, "missing"], [410, "unavailable"], [500, "server"], [503, "server"]] as const)("does not read private %s error bodies", async (status, reason) => {
    const cancelled = vi.fn();
    const response = new Response(new ReadableStream({ cancel: cancelled }), { status });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(response);
    await expect(open()).rejects.toEqual(new ArtifactPreviewError(reason));
    expect(cancelled).toHaveBeenCalledOnce();
  });

  it.each(["text/html", "image/svg+xml", "application/octet-stream", "", "text/plain-malicious"])("rejects unsupported %s before reading", async (type) => {
    const cancelled = vi.fn();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(new ReadableStream({ cancel: cancelled }), { headers: { "content-type": type } }));
    await expect(open()).rejects.toEqual(new ArtifactPreviewError("unsupported"));
    expect(cancelled).toHaveBeenCalledOnce();
  });

  it("rejects oversized Content-Length without reading and encodes path segments", async () => {
    expect(artifactURL("work/one", "file?x=1")).toMatch(/work%2Fone\/artifacts\/file%3Fx%3D1$/);
    const cancelled = vi.fn();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(new ReadableStream({ cancel: cancelled }), {
      headers: { "content-type": "text/plain", "content-length": String(MAX_PREVIEW_BYTES + 1) },
    }));
    await expect(open()).rejects.toEqual(new ArtifactPreviewError("tooLarge"));
    expect(cancelled).toHaveBeenCalledOnce();
  });

  it.each([undefined, "1"])("enforces actual byte size even with Content-Length %s", async (length) => {
    const cancelled = vi.fn();
    const headers = new Headers({ "content-type": "text/plain" });
    if (length) headers.set("content-length", length);
    const body = new ReadableStream({ start(c) { c.enqueue(new Uint8Array(MAX_PREVIEW_BYTES)); c.enqueue(new Uint8Array(1)); }, cancel: cancelled });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(body, { headers }));
    await expect(open()).rejects.toEqual(new ArtifactPreviewError("tooLarge"));
    expect(cancelled).toHaveBeenCalledOnce();
    expect(body.locked).toBe(false);
  });

  it("accepts the exact limit and an empty text body", async () => {
    const fetcher = vi.spyOn(globalThis, "fetch");
    fetcher.mockResolvedValueOnce(new Response(new Uint8Array(MAX_PREVIEW_BYTES), { headers: { "content-type": "text/plain" } }));
    await expect(open()).resolves.toMatchObject({ kind: "text", size: MAX_PREVIEW_BYTES });
    fetcher.mockResolvedValueOnce(new Response(null, { headers: { "content-type": "text/plain" } }));
    await expect(open()).resolves.toMatchObject({ kind: "text", size: 0, text: "" });
  });

  it("decodes split UTF-8 chunks and rejects invalid bytes", async () => {
    const bytes = new TextEncoder().encode("한글");
    const body = new ReadableStream({ start(c) { c.enqueue(bytes.slice(0, 1)); c.enqueue(bytes.slice(1)); c.close(); } });
    const fetcher = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response(body, { headers: { "content-type": "text/plain" } }));
    await expect(open()).resolves.toMatchObject({ text: "한글" });
    expect(body.locked).toBe(false);
    fetcher.mockResolvedValueOnce(new Response(new Uint8Array([255]), { headers: { "content-type": "text/plain" } }));
    await expect(open()).rejects.toEqual(new ArtifactPreviewError("invalid"));
  });

  it("sanitizes transport failures", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("private transport details"));
    await expect(open()).rejects.toEqual(new ArtifactPreviewError("network"));
  });

  it("cancels pending reads on timeout and releases their reader", async () => {
    vi.useFakeTimers();
    let body: ReadableStream;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, init) => {
      body = new ReadableStream({ start(c) { init!.signal!.addEventListener("abort", () => c.error(init!.signal!.reason)); } });
      return new Response(body, { headers: { "content-type": "text/plain" } });
    });
    const result = expect(open()).rejects.toEqual(new ArtifactPreviewError("timeout"));
    await vi.advanceTimersByTimeAsync(PREVIEW_TIMEOUT_MS);
    await result;
    expect(body!.locked).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("propagates caller cancellation and does not fetch after cancellation", async () => {
    const controller = new AbortController();
    const fetcher = vi.spyOn(globalThis, "fetch").mockImplementation((_url, init) => new Promise((_done, fail) => {
      init!.signal!.addEventListener("abort", () => fail(init!.signal!.reason));
    }));
    const result = open(controller.signal);
    controller.abort();
    await expect(result).rejects.toMatchObject({ name: "AbortError" });
    await expect(open(controller.signal)).rejects.toMatchObject({ name: "AbortError" });
    expect(fetcher).toHaveBeenCalledOnce();
  });
});
