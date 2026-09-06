import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { createInterface } from "node:readline";
import { expect, test } from "./fixtures";

// Only the credential-bearing trace is excluded; error checks and screenshots stay enabled.
test.use({ trace: "off" });

test("closing EventSource during a real query returns every database connection", async ({ page }) => {
  const root = resolve(process.cwd(), "../..");
  const child = spawn(process.env.KELPIE_E2E_PYTHON || resolve(root, ".venv/bin/python"),
    [resolve(root, "apps/web/e2e/stream-runtime.py")], {
      cwd: root,
      env: { NODE_ENV: "test", PATH: process.env.PATH, PYTHONPATH: `${root}/apps/api:${root}/apps/api/tests` },
      stdio: ["pipe", "pipe", "pipe"],
    });
  child.stderr.resume(); // Do not publish private service diagnostics or scoped credentials.
  const exited = new Promise<number | null>((done) => child.once("close", done));
  const output = createInterface({ input: child.stdout });
  try {
    const { bootstrapUrl } = await new Promise<{ bootstrapUrl: string }>((done, fail) => {
      const timeout = setTimeout(() => fail(new Error("Stream runtime readiness timed out")), 15_000);
      const onError = () => { clearTimeout(timeout); fail(new Error("Stream runtime failed to start")); };
      child.once("error", onError);
      child.once("close", onError);
      output.once("line", (line) => {
        clearTimeout(timeout);
        child.off("error", onError);
        child.off("close", onError);
        try { done(JSON.parse(line)); } catch { fail(new Error("Invalid stream runtime readiness")); }
      });
    });
    const state = async () => {
      const response = await page.request.get(`${bootstrapUrl}/state`);
      expect(response.status()).toBe(200);
      return await response.json();
    };
    await page.goto(bootstrapUrl);
    for (let count = 1; count <= 4; count++) {
      expect((await page.request.post(`${bootstrapUrl}/arm`)).status()).toBe(200);
      await page.getByRole("button", { name: "Connect", exact: true }).click();
      await expect.poll(state, { intervals: [25, 50, 100] }).toMatchObject({ paused: true, pauses: count });
      await page.getByRole("button", { name: "Disconnect", exact: true }).click();
      const release = await page.request.post(`${bootstrapUrl}/release`);
      expect(release.status()).toBe(200);
      expect(await release.json(), "Disconnect must occur during the database query").toEqual({ was_paused: true });
      await expect.poll(state).toMatchObject({ active: 0, checked_out: 0, started: count, closed: count });
    }
    await page.getByRole("button", { name: "Connect", exact: true }).click();
    await expect(page.locator("#events")).toContainText('"event_type":"work.created"');
    await page.getByRole("button", { name: "Disconnect", exact: true }).click();
    await expect.poll(state).toMatchObject({ active: 0, checked_out: 0, started: 5, closed: 5 });
  } finally {
    await page.goto("about:blank");
    output.close();
    child.stdin.end();
    const terminate = setTimeout(() => child.kill("SIGTERM"), 5_000);
    try {
      expect(await exited, "Disposable stream services and their logs must be clean").toBe(0);
    } finally {
      clearTimeout(terminate);
    }
  }
});
