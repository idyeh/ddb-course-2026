import { existsSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL, fileURLToPath } from "node:url";
import { chromium } from "playwright";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const distDir = join(root, "dist");
const bundledBrowser = chromium.executablePath();
const macChrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const browserPath = existsSync(bundledBrowser) ? bundledBrowser : macChrome;
const browser = await chromium.launch({ headless: true, executablePath: browserPath });
let failures = 0;

try {
  for (const name of readdirSync(distDir).filter((f) => f.endsWith(".html")).sort()) {
    const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
    await page.goto(pathToFileURL(join(distDir, name)).href, { waitUntil: "load" });
    const issues = await page.locator("section").evaluateAll((slides) => slides.flatMap((slide, index) => {
      const overflow = slide.scrollWidth > slide.clientWidth + 1 || slide.scrollHeight > slide.clientHeight + 1;
      const boxes = [...slide.querySelectorAll("h1,h2,h3,p,li,pre,table,img")];
      const root = slide.getBoundingClientRect();
      const outside = boxes.some((node) => {
        const rect = node.getBoundingClientRect();
        return rect.right > root.right + 2 || rect.bottom > root.bottom + 2 || rect.left < root.left - 2;
      });
      const centered = slide.classList.contains("lead") || slide.classList.contains("section");
      const title = slide.querySelector(":scope > h1, :scope > h2");
      const titleOffset = title ? title.getBoundingClientRect().top - root.top : null;
      const titleMisaligned = !centered && titleOffset !== null && Math.abs(titleOffset - 54) > 2;
      return overflow || outside || titleMisaligned
        ? [{ slide: index + 1, overflow, outside, titleMisaligned, titleOffset }]
        : [];
    }));
    if (issues.length) {
      failures += issues.length;
      for (const issue of issues) {
        if (issue.titleMisaligned) {
          console.error(`${name} slide ${issue.slide}: title offset is ${issue.titleOffset}px; expected 54px`);
        } else {
          console.error(`${name} slide ${issue.slide}: rendered content exceeds slide bounds`);
        }
      }
    }
    await page.close();
  }
} finally {
  await browser.close();
}

if (failures > 0) process.exit(1);
console.log("Rendered HTML overflow QA passed.");
