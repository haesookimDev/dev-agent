import { defineConfig, devices } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { tmpdir } from "node:os";
import { join } from "node:path";

// Inherited by the owned service and test workers, never discovered from a live deployment.
process.env.KELPIE_E2E_BUDGET_METADATA ??= join(tmpdir(), `kelpie-budget-${randomUUID()}.json`);

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: "http://127.0.0.1:13100",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: "node e2e/start-services.mjs",
      url: "http://127.0.0.1:18100/readyz",
      timeout: 60_000,
      reuseExistingServer: false,
      gracefulShutdown: { signal: "SIGTERM", timeout: 5_000 },
    },
    {
      command: "npm run dev -- --hostname 127.0.0.1 --port 13100",
      url: "http://127.0.0.1:13100/en",
      timeout: 60_000,
      reuseExistingServer: false,
      env: {
        KELPIE_API_URL: "http://127.0.0.1:18100",
        NEXT_PUBLIC_KELPIE_API_URL: "http://127.0.0.1:18100",
        NEXT_TELEMETRY_DISABLED: "1",
      },
    },
  ],
});
