import { afterEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "./browser-api";

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
});
