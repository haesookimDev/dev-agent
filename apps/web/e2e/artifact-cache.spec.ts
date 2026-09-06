import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { createInterface } from "node:readline";
import { expect, test } from "./fixtures";

// No credential-bearing trace; retain page-error checks, screenshots and shared cleanup.
test.use({ trace: "off" });

test("artifact recovery and access changes are not hidden by cached responses", async ({ page }) => {
  const root = resolve(process.cwd(), "../..");
  const child = spawn(process.env.KELPIE_E2E_PYTHON || resolve(root, ".venv/bin/python"),
    [resolve(root, "apps/web/e2e/artifact-cache-runtime.py")], {
      cwd: root,
      env: { NODE_ENV: "test", PATH: process.env.PATH, PYTHONPATH: `${root}/apps/api:${root}/apps/api/tests` },
      stdio: ["pipe", "pipe", "pipe"],
    });
  child.stderr.resume();
  const exited = new Promise<number | null>((done) => child.once("close", done));
  const output = createInterface({ input: child.stdout });
  try {
    const evidence = await new Promise<{ bootstrapUrl: string; artifactUrl: string; listUrl: string }>((done, fail) => {
      const timeout = setTimeout(() => fail(new Error("Cache runtime readiness timed out")), 15_000);
      const onError = () => { clearTimeout(timeout); fail(new Error("Cache runtime failed to start")); };
      child.once("error", onError);
      child.once("close", onError);
      output.once("line", (line) => {
        clearTimeout(timeout);
        child.off("error", onError);
        child.off("close", onError);
        try { done(JSON.parse(line)); } catch { fail(new Error("Invalid cache runtime readiness")); }
      });
    });
    const { bootstrapUrl, artifactUrl, listUrl } = evidence;
    const observed: { status: number; requestId: string; allowedOrigin?: string }[] = [];
    page.on("response", (response) => {
      if (response.url() === artifactUrl) observed.push({ status: response.status(),
        requestId: response.headers()["x-request-id"],
        allowedOrigin: response.headers()["access-control-allow-origin"] });
    });
    const fetchEvidence = async (url = artifactUrl) => {
      try {
        return await page.evaluate(async (target) => {
          // Keep the browser's default HTTP cache: no nonce, routing, cache override or mock.
          const response = await fetch(target, { credentials: "include" });
          return { status: response.status, requestId: response.headers.get("x-request-id"),
            body: await response.text() };
        }, url);
      } catch {
        // Only selected non-secret headers, never cookies, credentials or private response bodies.
        throw new Error(`Artifact fetch failed; response metadata: ${JSON.stringify(observed)}`);
      }
    };
    await page.goto(`${bootstrapUrl}/own`);
    const pending = page.waitForResponse((response) => response.url() === artifactUrl && response.request().isNavigationRequest());
    await page.getByRole("link", { name: "Open evidence" }).click();
    const unavailable = await pending;
    expect(unavailable.status()).toBe(410);
    const requestIds = new Set([unavailable.headers()["x-request-id"]]);
    // Restore only our synthetic file while retaining the exact artifact URL.
    expect((await page.request.post(`${bootstrapUrl}/restore`)).status()).toBe(204);
    await page.goto(`${bootstrapUrl}/own`);
    const restored = await fetchEvidence();
    expect(restored.status).toBe(200);
    expect(restored.body).toBe("Owned artifact acceptance evidence\n");
    const fresh = (response: { requestId: string | null }) => {
      expect(response.requestId).toBeTruthy();
      expect(requestIds.has(response.requestId!)).toBe(false);
      requestIds.add(response.requestId!);
    };
    fresh(restored);
    const listed = await fetchEvidence(listUrl);
    expect(listed.status).toBe(200);
    expect(listed.body).toContain("owned-evidence.txt");
    fresh(listed);
    for (const [identity, status] of [["foreign", 404], ["signed-out", 401]] as const) {
      await page.goto(`${bootstrapUrl}/${identity}`);
      for (const url of [artifactUrl, listUrl]) {
        const denied = await fetchEvidence(url);
        expect(denied.status).toBe(status);
        expect(denied.body).not.toContain("Owned artifact acceptance evidence");
        expect(denied.body).not.toContain("owned-evidence.txt");
        fresh(denied);
      }
    }
    await page.goto(`${bootstrapUrl}/own`);
    fresh(await fetchEvidence());
    expect((await page.request.post(`${bootstrapUrl}/remove`)).status()).toBe(204);
    const missing = await fetchEvidence();
    expect(missing.status).toBe(410);
    fresh(missing);
    expect((await page.request.post(`${bootstrapUrl}/restore`)).status()).toBe(204);
    const recovered = await fetchEvidence();
    expect(recovered.status).toBe(200);
    fresh(recovered);
    expect((await page.request.post(`${bootstrapUrl}/revoke`)).status()).toBe(204);
    for (const url of [artifactUrl, listUrl]) {
      const denied = await fetchEvidence(url);
      expect(denied.status).toBe(403);
      expect(denied.body).not.toContain("owned-evidence.txt");
      fresh(denied);
    }
  } finally {
    try { await page.goto("about:blank"); } finally {
      output.close();
      child.stdin.end();
      const terminate = setTimeout(() => child.kill("SIGTERM"), 5_000);
      try {
        expect(await exited, "Disposable cache services must stop with clean logs").toBe(0);
      } finally { clearTimeout(terminate); }
    }
  }
});
