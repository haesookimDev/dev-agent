import { defineConfig, devices } from "@playwright/test";

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
