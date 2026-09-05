import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { randomBytes } from "node:crypto";
import { request } from "node:http";
import { createServer } from "node:https";
import { connect } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const temporary = mkdtempSync(join(tmpdir(), "kelpie-preview-browser-"));
const python = process.env.KELPIE_E2E_PYTHON || join(root, ".venv/bin/python");
const environment = {
  ...process.env,
  KELPIE_PREVIEW_TEST_DIRECTORY: temporary,
  DATABASE_URL: `sqlite+aiosqlite:///${join(temporary, "test.db")}`,
  DATABASE_SCHEMA_MODE: "validate", AUTH_MODE: "oidc", WORKER_AUTH_MODE: "scoped",
  OIDC_ISSUER_URL: "https://localhost:19443", OIDC_CLIENT_ID: "preview-test",
  OIDC_REDIRECT_URI: "https://localhost:18443/auth/callback",
  OIDC_CLIENT_SECRET: "", OIDC_CLIENT_SECRET_FILE: "", OIDC_COOKIE_SECURE: "true",
  DASHBOARD_URL: "https://localhost:13443", CORS_ORIGINS: "https://localhost:13443",
  PREVIEW_ACCESS_ENABLED: "true", PREVIEW_DOMAIN: "preview.localhost",
  PREVIEW_HTTPS_PORT: "14443", PREVIEW_ALLOWED_CIDRS: "127.0.0.0/8",
  GATEWAY_SECRET: randomBytes(32).toString("hex"), GATEWAY_SECRET_FILE: "",
  ARTIFACT_ROOT: join(temporary, "artifacts"), SSL_CERT_FILE: join(temporary, "tls.crt"),
  WORKER_SHARED_SECRET: "", WORKER_SHARED_SECRET_FILE: "",
  GITHUB_APP_ID: "", GITHUB_PRIVATE_KEY_PATH: "", SLACK_BOT_TOKEN: "", SLACK_CHANNEL_ID: "",
  OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: "", NEXT_TELEMETRY_DISABLED: "1",
  KELPIE_API_URL: "http://127.0.0.1:18530", NEXT_PUBLIC_KELPIE_API_URL: "https://localhost:18443",
};
const children = [];
const servers = [];
const tunnels = new Set();
let stopping = false;

async function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  for (const socket of tunnels) socket.destroy();
  for (const server of servers) { server.closeAllConnections(); server.close(); }
  await Promise.all(children.map((child) => new Promise((done) => {
    if (child.exitCode !== null || child.signalCode !== null) return done();
    const force = setTimeout(() => child.kill("SIGKILL"), 2000);
    child.once("exit", () => { clearTimeout(force); done(); });
    child.kill("SIGTERM");
  })));
  // Only this invocation's freshly created private directory is removed.
  rmSync(temporary, { recursive: true, force: true });
  process.exit(code);
}
function run(command, arguments_) {
  const result = spawnSync(command, arguments_, { cwd: root, env: environment, stdio: "inherit" });
  if (result.error || result.status !== 0) throw new Error("Preview fixture setup failed");
}
function start(command, arguments_, cwd = root, env = environment) {
  const child = spawn(command, arguments_, { cwd, env, stdio: "inherit" });
  children.push(child);
  child.once("error", () => void stop(1));
  child.once("exit", () => { if (!stopping) void stop(1); });
}
async function ready(url) {
  for (let attempt = 0; attempt < 200; attempt++) {
    try { if ((await fetch(url)).ok) return; } catch { /* Startup only. */ }
    await new Promise((done) => setTimeout(done, 100));
  }
  throw new Error("Preview fixture did not become ready");
}
function proxy(port, target) {
  const server = createServer({ key: readFileSync(join(temporary, "tls.key")),
    cert: readFileSync(join(temporary, "tls.crt")) }, (incoming, outgoing) => {
    const upstream = request({ hostname: "127.0.0.1", port: target, path: incoming.url,
      method: incoming.method, headers: incoming.headers }, (response) => {
      outgoing.writeHead(response.statusCode, response.headers);
      outgoing.flushHeaders();
      response.pipe(outgoing);
    });
    upstream.on("error", () => { if (!outgoing.headersSent) outgoing.writeHead(502); outgoing.end(); });
    outgoing.on("close", () => upstream.destroy());
    incoming.pipe(upstream);
  });
  servers.push(server);
  // Next's development bootstrap needs its HMR WebSocket through this TLS proxy too.
  server.on("upgrade", (incoming, socket, head) => {
    const upstream = connect(target, "127.0.0.1");
    tunnels.add(socket); tunnels.add(upstream);
    for (const stream of [socket, upstream]) {
      stream.on("error", () => { socket.destroy(); upstream.destroy(); });
      stream.on("close", () => { tunnels.delete(stream); socket.destroy(); upstream.destroy(); });
    }
    upstream.once("connect", () => {
      let headers = `${incoming.method} ${incoming.url} HTTP/1.1\r\n`;
      for (let index = 0; index < incoming.rawHeaders.length; index += 2) {
        headers += `${incoming.rawHeaders[index]}: ${incoming.rawHeaders[index + 1]}\r\n`;
      }
      upstream.write(`${headers}\r\n`);
      if (head.length) upstream.write(head);
      socket.pipe(upstream).pipe(socket);
    });
  });
  server.on("error", () => void stop(1));
  server.listen(port, "127.0.0.1");
}
for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) process.on(signal, () => void stop());
try {
  run(python, ["apps/web/e2e/preview-services.py", "initialize"]);
  run(python, ["-m", "alembic", "-c", "apps/api/alembic.ini", "upgrade", "head"]);
  run(python, ["-m", "app.iam", join(temporary, "policy.json")]);
  run(python, ["-m", "app.worker_admin", "issue", "--worker-name", "preview-browser-worker",
    "--reason", "isolated OIDC browser acceptance", "--output", join(temporary, "worker-token")]);
  const gateway = join(temporary, "gateway");
  run("go", ["-C", "apps/gateway", "build", "-o", gateway, "."]);
  start(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "18530", "--no-access-log"]);
  await ready("http://127.0.0.1:18530/readyz");
  start(python, ["apps/web/e2e/preview-services.py"]);
  start(gateway, [], root, { ...environment, KELPIE_GATEWAY_LISTEN: "127.0.0.1:14443",
    KELPIE_CONTROL_URL: "http://127.0.0.1:18530", KELPIE_GATEWAY_AUTH_MODE: "oidc",
    KELPIE_GATEWAY_TOKEN: environment.GATEWAY_SECRET,
    KELPIE_GATEWAY_TLS_CERT_FILE: join(temporary, "tls.crt"),
    KELPIE_GATEWAY_TLS_KEY_FILE: join(temporary, "tls.key") });
  start(process.execPath, ["node_modules/next/dist/bin/next",
    process.env.KELPIE_PREVIEW_PRODUCTION === "1" ? "start" : "dev",
    "--hostname", "127.0.0.1", "--port", "13530"], join(root, "apps/web"));
  proxy(18443, 18530); proxy(19443, 19330); proxy(13443, 13530);
  await ready("http://127.0.0.1:13530/en");
  console.log("Isolated OIDC Preview services ready at https://localhost:13443/en");
} catch {
  console.error("Preview fixture startup failed; no credentials are printed.");
  await stop(1);
}
