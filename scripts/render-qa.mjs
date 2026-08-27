import { existsSync, mkdirSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL, fileURLToPath } from "node:url";
import { chromium } from "playwright";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const distDir = join(root, "dist");
const outputRoot = join(root, ".qa", "slides");
const bundledBrowser = chromium.executablePath();
const macChrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const browserPath = existsSync(bundledBrowser) ? bundledBrowser : macChrome;
const force = process.argv.includes("--force");
const from = process.argv.find((argument) => argument.startsWith("--from="))?.split("=", 2)[1];

mkdirSync(outputRoot, { recursive: true });

const browser = await chromium.launch({ headless: true, executablePath: browserPath });
try {
  const decks = readdirSync(distDir)
    .filter((name) => /^\d{2}-.*\.html$/.test(name))
    .filter((name) => !from || name >= from)
    .sort();

  for (const deck of decks) {
    const stem = deck.replace(/\.html$/, "");
    const deckDir = join(outputRoot, stem);
    mkdirSync(deckDir, { recursive: true });
    const page = await browser.newPage({
      viewport: { width: 1280, height: 720 },
      deviceScaleFactor: 1,
    });
    await page.goto(pathToFileURL(join(distDir, deck)).href, { waitUntil: "load" });
    await page.evaluate(() => document.fonts.ready);
    const count = await page.locator("section").count();
    await page.keyboard.press("Home");
    for (let index = 0; index < count; index += 1) {
      const number = String(index + 1).padStart(2, "0");
      const output = join(deckDir, `${number}.png`);
      if (force || !existsSync(output)) await page.screenshot({ path: output });
      if (index + 1 < count) {
        await page.keyboard.press("ArrowRight");
        await page.waitForTimeout(25);
      }
    }
    await page.close();
    console.log(`${stem}: rendered ${count} slides`);
  }
} finally {
  await browser.close();
}
