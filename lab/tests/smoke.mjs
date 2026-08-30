/* Headless runner for web/smoke.js.

   The Python side of the lab stays dependency-free; this optional runner reuses
   the Playwright the courseware already installs for its slide checks.

       corepack pnpm test:lab:browser

   It starts the lab server itself on an unused port and shuts it down again. */

import { spawn } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const labRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const port = Number(process.env.LAB_SMOKE_PORT ?? 8731);
const base = `http://127.0.0.1:${port}/`;

const server = spawn("python3", ["run.py", "serve", "--no-open", "--port", String(port)], {
  cwd: labRoot, stdio: ["ignore", "ignore", "inherit"],
});

async function waitForServer(timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${base}api/scenarios`);
      if (response.ok) return;
    } catch { /* not up yet */ }
    await new Promise((done) => setTimeout(done, 150));
  }
  throw new Error(`lab server did not start on ${base}`);
}

let exitCode = 1;
let browser;
try {
  await waitForServer();
  browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  const consoleErrors = [];
  page.on("pageerror", (error) => consoleErrors.push(String(error)));
  await page.goto(base, { waitUntil: "networkidle" });

  const report = await page.evaluate(async () => {
    const module = await import("./smoke.js");
    return module.runSmoke();
  }, { timeout: 180000 });

  for (const failure of report.failures) {
    console.error(`  FAIL ${failure.name}${failure.detail ? ` — ${failure.detail}` : ""}`);
  }
  for (const error of consoleErrors) console.error(`  FAIL uncaught: ${error}`);

  const failed = report.failed + consoleErrors.length;
  console.log(`浏览器冒烟测试：${report.total} 项断言，${failed} 项失败。`);
  exitCode = failed === 0 ? 0 : 1;
} catch (error) {
  if (/Executable doesn't exist/.test(error.message)) {
    console.error("找不到 Chromium。先安装一次：corepack pnpm browser:install");
  } else {
    console.error(error.message);
  }
} finally {
  await browser?.close();
  server.kill();
}
process.exit(exitCode);
