import { existsSync, mkdirSync, readdirSync, writeFileSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const sourceDir = join(root, "diagrams");
const outputDir = join(root, "assets", "diagrams");
const mmdc = join(root, "node_modules", ".bin", process.platform === "win32" ? "mmdc.cmd" : "mmdc");
const qaDir = join(root, ".qa");
const puppeteerConfig = join(qaDir, "puppeteer-config.json");

const { chromium } = await import("playwright");
const bundledBrowser = chromium.executablePath();
const macChrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const browserPath = existsSync(bundledBrowser) ? bundledBrowser : macChrome;

if (!existsSync(browserPath)) {
  throw new Error("No Chromium-compatible browser found. Install Google Chrome or run `pnpm exec playwright install chromium`.");
}

mkdirSync(outputDir, { recursive: true });
mkdirSync(qaDir, { recursive: true });
writeFileSync(puppeteerConfig, JSON.stringify({
  executablePath: browserPath,
  headless: true
}, null, 2));

for (const name of readdirSync(sourceDir).filter((f) => f.endsWith(".mmd")).sort()) {
  const output = join(outputDir, `${basename(name, ".mmd")}.svg`);
  const result = spawnSync(mmdc, [
    "--input", join(sourceDir, name),
    "--output", output,
    "--backgroundColor", "transparent",
    "--theme", "neutral",
    "--width", "1100",
    "--puppeteerConfigFile", puppeteerConfig
  ], { cwd: root, stdio: "inherit" });
  if (result.status !== 0) process.exit(result.status ?? 1);
}
