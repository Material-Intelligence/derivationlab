import { spawn } from "node:child_process";
import { access, mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const packageDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repoDir = path.resolve(packageDir, "../..");
const evidenceDir = process.env.SMOKE_EVIDENCE_DIR
  ? path.resolve(repoDir, process.env.SMOKE_EVIDENCE_DIR)
  : path.join(repoDir, "runs/smoke/server-auth-browser");
const candidates = [
  process.env.CHROME_PATH,
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
].filter(Boolean);
const passwords = {
  admin: "admin-server-smoke-private",
  bob: "bob-server-smoke-private",
  reset: "bob-server-smoke-reset-private",
};

function availablePort() {
  return new Promise((resolve, reject) => {
    const socket = net.createServer();
    socket.unref();
    socket.on("error", reject);
    socket.listen(0, "127.0.0.1", () => {
      const address = socket.address();
      socket.close(() => resolve(address.port));
    });
  });
}

function run(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { ...options, stdio: ["ignore", "pipe", "pipe"] });
    let output = "";
    child.stdout.on("data", (chunk) => { output += chunk; });
    child.stderr.on("data", (chunk) => { output += chunk; });
    child.on("error", reject);
    child.on("exit", (code) => code === 0 ? resolve(output) : reject(new Error(`${command} exited ${code}: ${output}`)));
  });
}

async function submitLogin(page, identifier, password) {
  await page.getByLabel("Username or email").fill(identifier);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
}

async function login(page, identifier, password) {
  await submitLogin(page, identifier, password);
  await page.getByText(new RegExp(`Signed in as\\s+${identifier}`)).waitFor();
}

let executablePath;
for (const candidate of candidates) {
  try { await access(candidate); executablePath = candidate; break; } catch { /* try next */ }
}
if (!executablePath) throw new Error("No supported system Chrome/Edge binary found. Set CHROME_PATH.");

const temporaryRoot = await mkdtemp(path.join(os.tmpdir(), "derivationlab-server-smoke-"));
const port = await availablePort();
const url = `https://127.0.0.1:${port}`;
const cert = path.join(temporaryRoot, "cert.pem");
const key = path.join(temporaryRoot, "key.pem");
const opensslConfig = path.join(temporaryRoot, "openssl.cnf");
await writeFile(opensslConfig, "[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n[dn]\nCN=127.0.0.1\n[v3]\nsubjectAltName=IP:127.0.0.1\n");
await run("openssl", ["req", "-x509", "-nodes", "-newkey", "rsa:2048", "-days", "1", "-keyout", key, "-out", cert, "-config", opensslConfig]);
await mkdir(evidenceDir, { recursive: true });

const server = spawn("uv", [
  "run", "--frozen", "--project", "src/derivation_api", "python",
  "src/derivation_app/tests/server_auth_smoke_app.py",
  "--port", String(port),
  "--server-root", path.join(temporaryRoot, "server"),
  "--web-dist", path.join(packageDir, "dist"),
  "--cert", cert,
  "--key", key,
], {
  cwd: repoDir,
  env: { ...process.env, PYTHONPATH: "src:src/derivation_api" },
  stdio: ["ignore", "pipe", "pipe"],
});
let serverOutput = "";
server.stdout.on("data", (chunk) => { serverOutput += chunk; });
server.stderr.on("data", (chunk) => { serverOutput += chunk; });

let browser;
const evidence = {
  schema_version: "derivationlab-server-auth-browser-smoke-v1",
  origin: "https://127.0.0.1:<ephemeral>",
  assertions: {
    unauthenticated_gate: false,
    independent_browser_sessions: false,
    password_reset_revokes_session: false,
    permanent_password_login: false,
    account_disable_revokes_session: false,
    administrator_content_panel_opened: false,
  },
};

try {
  browser = await chromium.launch({ executablePath, headless: true });
  const adminContext = await browser.newContext({ ignoreHTTPSErrors: true });
  const bobContext = await browser.newContext({ ignoreHTTPSErrors: true });
  const adminPage = await adminContext.newPage();
  const bobPage = await bobContext.newPage();

  for (let attempt = 0; attempt < 60; attempt += 1) {
    if (server.exitCode !== null) throw new Error(`Server exited with ${server.exitCode}: ${serverOutput}`);
    try {
      await adminPage.goto(url, { waitUntil: "networkidle", timeout: 1000 });
      break;
    } catch {
      if (attempt === 59) throw new Error(`HTTPS server did not start: ${serverOutput}`);
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }

  await adminPage.getByRole("heading", { name: "Sign in to DerivationLab" }).waitFor();
  evidence.assertions.unauthenticated_gate = true;

  await login(adminPage, "admin", passwords.admin);
  await bobPage.goto(url, { waitUntil: "networkidle" });
  await login(bobPage, "bob", passwords.bob);
  if (await adminContext.cookies(url).then((items) => items[0]?.value) === await bobContext.cookies(url).then((items) => items[0]?.value)) {
    throw new Error("Browser contexts did not receive independent sessions");
  }
  evidence.assertions.independent_browser_sessions = true;

  await adminPage.getByRole("button", { name: "Manage users" }).click();
  const bobRow = adminPage.locator(".site-account-row").filter({ hasText: "bob@example.test" });
  await bobRow.getByRole("button", { name: "View content" }).click();
  await adminPage.getByText("No derivations", { exact: true }).waitFor();
  await adminPage.getByRole("button", { name: "Close content view" }).click();
  evidence.assertions.administrator_content_panel_opened = true;
  const resetInput = bobRow.getByLabel("New permanent password: bob");
  await resetInput.fill(passwords.reset);
  await bobRow.getByRole("button", { name: "Set password" }).click();
  for (let attempt = 0; attempt < 40 && await resetInput.inputValue() !== ""; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  if (await resetInput.inputValue() !== "") throw new Error("Administrator password reset did not complete");
  await bobPage.reload({ waitUntil: "networkidle" });
  await bobPage.getByRole("heading", { name: "Sign in to DerivationLab" }).waitFor();
  evidence.assertions.password_reset_revokes_session = true;

  await login(bobPage, "bob", passwords.reset);
  evidence.assertions.permanent_password_login = true;

  const disableButton = bobRow.getByRole("button", { name: "Disable" });
  await disableButton.click();
  await disableButton.waitFor({ state: "detached" });
  await bobPage.reload({ waitUntil: "networkidle" });
  await bobPage.getByRole("heading", { name: "Sign in to DerivationLab" }).waitFor();
  evidence.assertions.account_disable_revokes_session = true;

  await writeFile(path.join(evidenceDir, "server-auth-browser-validation.json"), `${JSON.stringify(evidence, null, 2)}\n`);
  console.log(JSON.stringify({ marker: "DONE_SERVER_AUTH_BROWSER_SMOKE", ...evidence.assertions }));
} catch (error) {
  if (serverOutput) console.error(serverOutput);
  throw error;
} finally {
  await browser?.close();
  server.kill("SIGTERM");
  await rm(temporaryRoot, { recursive: true, force: true });
}
