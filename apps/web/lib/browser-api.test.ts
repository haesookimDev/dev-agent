import { afterEach, describe, expect, it, vi } from "vitest";
import { apiJSON, authenticatedFetch, BrowserAPIError, requestErrorMessage } from "./browser-api";

describe("authenticatedFetch", () => {
  afterEach(() => vi.restoreAllMocks());

  it("includes the OIDC session cookie on browser API requests", async () => {
    const response = new Response(null, { status: 204 });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(response);

    await authenticatedFetch("https://control.example/api/work-items", { method: "POST" });

    expect(fetchMock).toHaveBeenCalledWith(
      "https://control.example/api/work-items",
      { method: "POST", credentials: "include" },
    );
  });

  it("parses successful JSON and rejects HTTP errors before using their payload", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock.mockResolvedValueOnce(Response.json({ id: "work" }));
    await expect(apiJSON("/api/work-items")).resolves.toEqual({ id: "work" });
    fetchMock.mockResolvedValueOnce(Response.json({ detail: "private internal detail" }, { status: 403 }));
    await expect(apiJSON("/api/work-items")).rejects.toEqual(new BrowserAPIError(403));
  });

  it("provides safe localized permission and network errors", () => {
    expect(requestErrorMessage(new BrowserAPIError(403), "failed", "offline", "denied")).toBe("denied");
    expect(requestErrorMessage(new BrowserAPIError(500), "failed", "offline", "denied")).toBe("failed (500)");
    expect(requestErrorMessage(new TypeError("private details"), "failed", "offline", "denied")).toBe("offline");
  });
});
