import { describe, expect, it } from "vitest";
import { previewExchangeURL } from "./preview";

const launch = { launch_code: `kpl_${"a".repeat(43)}`, exchange_url: "https://work.preview.example.net:8443/_kelpie/authorize", expires_at: "2026-09-06T00:00:00Z" };

describe("Preview launch validation", () => {
  it("keeps an HTTPS, work-scoped POST endpoint including its configured port", () => {
    expect(previewExchangeURL(launch, "work")).toBe(launch.exchange_url);
  });
  it.each([
    { exchange_url: "http://work.preview.example.net/_kelpie/authorize" },
    { exchange_url: "https://other.preview.example.net/_kelpie/authorize" },
    { exchange_url: "https://work.preview.example.net/_kelpie/authorize?code=credential" },
    { exchange_url: "https://work.preview.example.net/_kelpie/authorize#credential" },
    { exchange_url: "https://user@work.preview.example.net/_kelpie/authorize" },
    { exchange_url: "https://work.preview.example.net/" },
    { launch_code: "unexpected" }, { expires_at: "invalid" },
  ])("rejects an unsafe or malformed response without reflecting it", (change) => {
    expect(() => previewExchangeURL({ ...launch, ...change }, "work")).toThrow("Invalid Preview launch response");
  });
});
