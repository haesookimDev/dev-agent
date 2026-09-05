import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const temporary = mkdtempSync(join(tmpdir(), "kelpie-browser-test-"));
const python = process.env.KELPIE_E2E_PYTHON || join(root, ".venv/bin/python");
const tokenFile = join(temporary, "worker-token");
const environment = {
  ...process.env,
  DATABASE_URL: `sqlite+aiosqlite:///${join(temporary, "test.db")}`,
  DATABASE_SCHEMA_MODE: "validate",
  AUTH_MODE: "development",
  WORKER_AUTH_MODE: "scoped",
  DEVELOPMENT_ORGANIZATION: "browser-test",
  ARTIFACT_ROOT: join(temporary, "artifacts"),
  CORS_ORIGINS: "http://127.0.0.1:13100",
  WORKER_SHARED_SECRET: "",
  WORKER_SHARED_SECRET_FILE: "",
  GITHUB_APP_ID: "",
  GITHUB_PRIVATE_KEY_PATH: "",
  SLACK_BOT_TOKEN: "",
  SLACK_CHANNEL_ID: "",
  OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: "",
};
const children = [];
let stopping = false;

async function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  await Promise.all(children.map((child) => new Promise((done) => {
    if (child.exitCode !== null || child.signalCode !== null) return done();
    const force = setTimeout(() => child.kill("SIGKILL"), 2000);
    child.once("exit", () => { clearTimeout(force); done(); });
    child.kill("SIGTERM");
  })));
  // This exact directory was freshly created by mkdtemp for this invocation.
  rmSync(temporary, { recursive: true, force: true });
  process.exit(code);
}

function run(command, arguments_) {
  const result = spawnSync(command, arguments_, { cwd: root, env: environment, stdio: "inherit" });
  if (result.error || result.status !== 0) throw new Error(`Test service setup failed: ${command}`);
}

function start(command, arguments_, env) {
  const child = spawn(command, arguments_, { cwd: root, env, stdio: "inherit" });
  children.push(child);
  child.once("error", () => void stop(1));
  child.once("exit", () => { if (!stopping) void stop(1); });
  return child;
}

process.on("SIGTERM", () => void stop());
process.on("SIGINT", () => void stop());

try {
  run(python, ["-m", "alembic", "-c", "apps/api/alembic.ini", "upgrade", "head"]);
  run(python, ["-m", "app.worker_admin", "issue", "--worker-name", "browser-test-worker",
    "--reason", "isolated browser acceptance test", "--output", tokenFile]);
  const worker = join(temporary, "mock-worker");
  run("go", ["-C", "apps/worker", "build", "-o", worker, "./cmd/kelpie-worker"]);
  start(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "18100", "--no-access-log"], environment);
  for (let attempt = 0; ; attempt++) {
    try { if ((await fetch("http://127.0.0.1:18100/readyz")).ok) break; } catch { /* Startup only. */ }
    if (attempt === 100) throw new Error("API did not become ready");
    await new Promise((done) => setTimeout(done, 50));
  }
  start(worker, [], {
    ...environment,
    KELPIE_CONTROL_URL: "http://127.0.0.1:18100",
    KELPIE_WORKER_TOKEN: "",
    KELPIE_WORKER_TOKEN_FILE: tokenFile,
    KELPIE_WORKER_NAME: "browser-test-worker",
    KELPIE_EXECUTOR: "mock",
    KELPIE_CPU_TOTAL: "2",
    KELPIE_MEMORY_MB_TOTAL: "4096",
    KELPIE_DISK_GB_TOTAL: "30",
    KELPIE_RUN_CPU: "2",
    KELPIE_RUN_MEMORY_MB: "4096",
    KELPIE_RUN_DISK_GB: "30",
    KELPIE_POLL_SECONDS: "1",
    KELPIE_WORK_ROOT: join(temporary, "workspaces"),
  });
} catch (error) {
  console.error(error.message);
  await stop(1);
}
