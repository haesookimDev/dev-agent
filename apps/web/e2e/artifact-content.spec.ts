import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { createInterface } from "node:readline";
import { buffer } from "node:stream/consumers";
import { expect, test } from "./fixtures";

// Traces include HTTP headers: never publish the disposable scoped-auth cookies.
// Assertions, unhandled-error checks and failure screenshots remain enabled.
test.use({ trace: "off" });

test("artifact documents stay inert while valid evidence remains readable", async ({ page }) => {
  const root = resolve(process.cwd(), "../..");
  const child = spawn(process.env.KELPIE_E2E_PYTHON || resolve(root, ".venv/bin/python"),
    [resolve(root, "apps/web/e2e/artifact-runtime.py")], {
      cwd: root,
      env: { NODE_ENV: "test", PATH: process.env.PATH, PYTHONPATH: `${root}/apps/api:${root}/apps/api/tests` },
      stdio: ["pipe", "pipe", "pipe"],
    });
  child.stderr.resume(); // Service diagnostics may contain private runtime state.
  const exited = new Promise<number | null>((done) => child.once("close", done));
  const output = createInterface({ input: child.stdout });
  try {
    const evidence = await new Promise<{
      bootstrapUrl: string; apiUrl: string; work: string;
      artifacts: Record<string, string>; registration: number;
    }>((done, fail) => {
      const timeout = setTimeout(() => fail(new Error("Artifact runtime readiness timed out")), 15_000);
      const onError = () => { clearTimeout(timeout); fail(new Error("Artifact runtime failed to start")); };
      child.once("error", onError);
      child.once("close", onError);
      output.once("line", (line) => {
        clearTimeout(timeout);
        child.off("error", onError);
        child.off("close", onError);
        try { done(JSON.parse(line)); } catch { fail(new Error("Invalid artifact runtime readiness")); }
      });
    });
    expect(evidence.registration).toBe(415);
    const open = async (name: string) => {
      // A real localhost page/link supplies the same-site navigation context for Strict cookies.
      await page.goto(evidence.bootstrapUrl);
      const url = `${evidence.apiUrl}/api/work-items/${evidence.work}/artifacts/${evidence.artifacts[name]}`;
      const response = page.waitForResponse((item) => item.url() === url && item.request().isNavigationRequest());
      await page.getByRole("link", { name, exact: true }).click();
      await page.waitForURL(url);
      await page.waitForLoadState();
      return await response;
    };
    let probeBytes: Buffer | undefined;
    for (const [name, contentType] of [
      ["plain-probe.txt", "text/plain"], ["evidence.png", "image/png"], ["result.json", "application/json"],
    ]) {
      const response = await open(name);
      expect(response.status()).toBe(200);
      expect(response.headers()["content-type"]).toContain(contentType);
      expect(response.headers()["content-security-policy"]).toBe("sandbox");
      expect(response.headers()["x-content-type-options"]).toBe("nosniff");
      expect(await page.evaluate(() => window.origin)).toBe("null");
      expect(await page.locator("html").getAttribute("data-artifact-probe")).toBeNull();
      if (name === "plain-probe.txt") {
        probeBytes = await response.body();
        await expect(page.locator("body")).toContainText("<script>");
        await expect(page.locator("h1#probe, script")).toHaveCount(0);
      } else if (name === "evidence.png") {
        await expect.poll(() => page.locator("img").evaluate((node: HTMLImageElement) =>
          ({ complete: node.complete, width: node.naturalWidth, height: node.naturalHeight }),
        )).toEqual({ complete: true, width: 32, height: 32 });
      } else {
        await expect(page.locator("body")).toContainText('"result":"synthetic evidence"');
      }
    }
    for (const name of ["검증 결과 ✅.txt", "100%20 complete; v2.txt"]) {
      await page.goto(evidence.bootstrapUrl);
      const pending = page.waitForEvent("download");
      // Chromium's real link-download gesture parses the API's Content-Disposition itself.
      await page.getByRole("link", { name, exact: true }).click({ modifiers: ["Alt"] });
      const download = await pending;
      expect(download.suggestedFilename()).toBe(name);
      expect(await download.failure()).toBeNull();
      const stream = await download.createReadStream();
      if (!stream || !probeBytes) throw new Error("Downloaded filename evidence is missing");
      expect(await buffer(stream)).toEqual(probeBytes);
    }
    const denied = await open("unsupported-report.html");
    expect(denied.status()).toBe(410);
    expect(await denied.json()).toEqual({ detail: "artifact content is unavailable" });
    await expect(page.locator("h1#probe")).toHaveCount(0);
    expect(await page.locator("html").getAttribute("data-artifact-probe")).toBeNull();
  } finally {
    output.close();
    child.stdin.end();
    const terminate = setTimeout(() => child.kill("SIGTERM"), 5_000);
    try {
      expect(await exited, "Disposable artifact services must stop cleanly").toBe(0);
    } finally {
      clearTimeout(terminate);
    }
  }
});
