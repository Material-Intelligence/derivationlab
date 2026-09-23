import { spawn } from "node:child_process";
import { access, mkdir, writeFile } from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import axe from "axe-core";
import { chromium } from "playwright-core";

const packageDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repoDir = path.resolve(packageDir, "../..");
const evidenceOverride = process.env.SMOKE_EVIDENCE_DIR ?? process.env.DERIVATION_WEB_SMOKE_EVIDENCE_DIR;
const evidenceDir = evidenceOverride
  ? path.resolve(repoDir, evidenceOverride)
  : path.join(repoDir, "runs/smoke/fixture-smoke");
const candidates = [
  process.env.CHROME_PATH,
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
].filter(Boolean);

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

async function assertNoSeriousAxeViolations(page, label) {
  const violations = await page.evaluate(async () => {
    const result = await window.axe.run(document, { resultTypes: ["violations"] });
    return result.violations
      .filter((violation) => violation.impact === "serious" || violation.impact === "critical")
      .map((violation) => ({ id: violation.id, impact: violation.impact, nodes: violation.nodes.map((node) => node.target) }));
  });
  if (violations.length) throw new Error(`${label} axe violations: ${JSON.stringify(violations)}`);
  return { label, serious_or_critical: 0 };
}

async function assertModalFocusLoop(page, dialog, label) {
  const nativeState = await dialog.evaluate((element) => ({
    tag: element.tagName,
    open: element.hasAttribute("open"),
    focusInside: element.contains(document.activeElement),
  }));
  if (nativeState.tag !== "DIALOG" || !nativeState.open || !nativeState.focusInside) {
    throw new Error(`${label} is not an active native modal: ${JSON.stringify(nativeState)}`);
  }
  for (const key of ["Tab", "Tab", "Tab", "Shift+Tab", "Shift+Tab", "Shift+Tab"]) {
    await page.keyboard.press(key);
    if (!await dialog.evaluate((element) => element.contains(document.activeElement))) {
      throw new Error(`${label} focus escaped after ${key}`);
    }
  }
  return nativeState;
}

let executablePath;
for (const candidate of candidates) {
  try { await access(candidate); executablePath = candidate; break; } catch { /* try next */ }
}
if (!executablePath) throw new Error("No supported system Chrome/Edge binary found. Set CHROME_PATH.");

await mkdir(evidenceDir, { recursive: true });
const port = await availablePort();
const url = `http://127.0.0.1:${port}`;
const server = spawn(process.execPath, [path.join(packageDir, "node_modules/vite/bin/vite.js"), "--host", "127.0.0.1", "--port", String(port)], {
  cwd: packageDir,
  env: { ...process.env, VITE_API_MODE: "fixture" },
  stdio: ["ignore", "pipe", "pipe"],
});
let serverOutput = "";
server.stdout.on("data", (chunk) => { serverOutput += chunk; });
server.stderr.on("data", (chunk) => { serverOutput += chunk; });

const evidence = {
  schema_version: "derivationlab-fixture-browser-smoke-v3",
  assertions: { axe: [], dialogs: {} },
  screenshots: [],
};
let browser;
const pageErrors = [];

try {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    if (server.exitCode !== null) throw new Error(`Vite server exited with ${server.exitCode}: ${serverOutput}`);
    try {
      const response = await fetch(url);
      if (response.ok) break;
    } catch { /* server still starting */ }
    await new Promise((resolve) => setTimeout(resolve, 100));
    if (attempt === 59) throw new Error("Vite did not start within 6 seconds");
  }

  browser = await chromium.launch({ executablePath, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
  page.on("pageerror", (error) => pageErrors.push(error.stack ?? error.message));
  await page.addInitScript({ content: axe.source });
  await page.goto(`${url}/?run=demo-run`, { waitUntil: "networkidle" });
  await page.getByRole("heading", { name: "Complete derivation tree" }).waitFor();
  evidence.assertions.axe.push(await assertNoSeriousAxeViolations(page, "main workbench"));

  await page.getByRole("button", { name: "Open: Numerical counterexample check" }).click();
  await page.getByRole("heading", { name: "Result B: validity boundary" }).waitFor();
  await page.waitForTimeout(250);
  await page.evaluate(() => document.activeElement?.blur?.());
  await page.screenshot({ path: path.join(evidenceDir, "derivation-tree-desktop-node-route.png"), fullPage: true });
  evidence.screenshots.push("derivation-tree-desktop-node-route.png");

  const edgeControl = page.getByRole("button", { name: "Select matching route: Define states and constraints → Symmetry route" });
  await edgeControl.dispatchEvent("click");
  await edgeControl.focus();
  await page.getByRole("heading", { name: "Result C: symmetry closure" }).waitFor({ timeout: 3000 });
  await page.waitForTimeout(250);
  const selectedText = await page.locator('article[aria-current="step"]').innerText();
  if (!selectedText.includes("Symmetry route")) throw new Error("Edge click did not select the target route step");
  await page.screenshot({ path: path.join(evidenceDir, "derivation-tree-desktop-edge-route.png"), fullPage: true });
  evidence.screenshots.push("derivation-tree-desktop-edge-route.png");

  const detailsTrigger = page.getByRole("button", { name: "View 5 details" });
  await detailsTrigger.click();
  const detailsDialog = page.getByRole("dialog", { name: "Symmetry route" });
  await detailsDialog.waitFor({ state: "visible" });
  evidence.assertions.dialogs.details = await assertModalFocusLoop(page, detailsDialog, "details dialog");
  evidence.assertions.axe.push(await assertNoSeriousAxeViolations(page, "details dialog"));
  await page.keyboard.press("Escape");
  await detailsDialog.waitFor({ state: "detached" });
  if (!await detailsTrigger.evaluate((element) => document.activeElement === element)) throw new Error("Details dialog did not restore trigger focus");

  const branchTrigger = page.getByRole("button", { name: "Start a new branch here" });
  await branchTrigger.click();
  const branchDialog = page.getByRole("dialog", { name: /Start a new branch/ });
  await branchDialog.waitFor({ state: "visible" });
  evidence.assertions.dialogs.branch = await assertModalFocusLoop(page, branchDialog, "branch dialog");
  evidence.assertions.axe.push(await assertNoSeriousAxeViolations(page, "branch dialog"));
  await branchDialog.dispatchEvent("mousedown");
  await branchDialog.waitFor({ state: "detached" });
  if (!await branchTrigger.evaluate((element) => document.activeElement === element)) throw new Error("Branch dialog backdrop close did not restore trigger focus");

  const reportTrigger = page.getByRole("button", { name: "Export PDF" });
  await reportTrigger.click();
  const reportDialog = page.getByRole("dialog", { name: "Export auditable PDF" });
  await reportDialog.waitFor({ state: "visible" });
  evidence.assertions.dialogs.report = await assertModalFocusLoop(page, reportDialog, "report dialog");
  evidence.assertions.axe.push(await assertNoSeriousAxeViolations(page, "report dialog"));
  await page.keyboard.press("Escape");
  await reportDialog.waitFor({ state: "detached" });
  if (!await reportTrigger.evaluate((element) => document.activeElement === element)) throw new Error("Report dialog did not restore trigger focus");

  await page.setViewportSize({ width: 430, height: 900 });
  await page.getByRole("tab", { name: "Derivation tree" }).click();
  await page.getByRole("button", { name: "Open: Numerical counterexample check" }).click();
  await page.getByRole("tab", { name: "Route reading" }).click();
  await page.getByRole("heading", { name: "Result B: validity boundary" }).waitFor();
  await page.waitForTimeout(250);
  await page.screenshot({ path: path.join(evidenceDir, "derivation-tree-narrow-node-route.png"), fullPage: true });
  evidence.screenshots.push("derivation-tree-narrow-node-route.png");

  evidence.assertions.pageErrors = pageErrors;
  if (pageErrors.length) throw new Error(`Browser page errors: ${pageErrors.join(" | ")}`);
  await writeFile(path.join(evidenceDir, "fixture-browser-validation.json"), `${JSON.stringify(evidence, null, 2)}\n`);
  console.log(JSON.stringify({ marker: "DONE_BROWSER_SMOKE", ...evidence.assertions, screenshots: evidence.screenshots.length }));
} catch (error) {
  if (serverOutput) console.error(serverOutput);
  throw error;
} finally {
  await browser?.close();
  server.kill("SIGTERM");
}
