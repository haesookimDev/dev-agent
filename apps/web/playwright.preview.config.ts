import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e-preview",
  workers: 1, fullyParallel: false, forbidOnly: !!process.env.CI, retries: 0,
  timeout: 30_000, expect: { timeout: 10_000 },
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report/preview" }]],
  outputDir: "test-results/preview",
  use: {
    ...devices["Desktop Chrome"], baseURL: "https://localhost:13443",
    // Only this isolated browser context trusts this invocation's disposable TLS fixture.
    ignoreHTTPSErrors: true,
    // OIDC codes and cookie headers must not be retained in traces.
    trace: "off", screenshot: "only-on-failure",
  },
  webServer: {
    command: "node e2e/start-preview-services.mjs", url: "https://localhost:13443/en",
    ignoreHTTPSErrors: true, timeout: 60_000,
    reuseExistingServer: !process.env.CI && process.env.KELPIE_PREVIEW_REUSE === "1",
    gracefulShutdown: { signal: "SIGTERM", timeout: 5_000 },
  },
});
