import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const slidesDir = join(root, "slides");
let failures = 0;

function fail(file, slide, message) {
  failures += 1;
  console.error(`${file} slide ${slide}: ${message}`);
}

for (const name of readdirSync(slidesDir).filter((f) => f.endsWith(".md")).sort()) {
  const path = join(slidesDir, name);
  const source = readFileSync(path, "utf8");
  if (/^date\s*:/m.test(source)) fail(name, 0, "deck must not contain a date field");
  if (!/^author:\s*["']?Hong["']?\s*$/m.test(source)) fail(name, 0, "missing author Hong");

  const body = source.replace(/^---\n[\s\S]*?\n---\n/, "");
  const slides = body.split(/\n---\n/g);
  slides.forEach((slide, index) => {
    const number = index + 1;
    const visible = slide
      .replace(/<!--[\s\S]*?-->/g, "")
      .replace(/```[\s\S]*?```/g, "")
      .replace(/<[^>]+>/g, "")
      .replace(/^\s*#+\s*/gm, "")
      .trim();
    const chars = visible.replace(/\s/g, "").length;
    const bullets = (visible.match(/^\s*[-*+]\s+/gm) ?? []).length;
    const codeBlocks = [...slide.matchAll(/```[^\n]*\n([\s\S]*?)```/g)];
    const longestCode = Math.max(0, ...codeBlocks.map((m) => m[1].trimEnd().split("\n").length));
    const tableRows = (visible.match(/^\|.*\|\s*$/gm) ?? []).length;

    if (chars > 430) fail(name, number, `visible text is dense (${chars} non-space characters)`);
    if (bullets > 9) fail(name, number, `too many bullets (${bullets})`);
    if (longestCode > 18) fail(name, number, `code block is too long (${longestCode} lines)`);
    if (tableRows > 9) fail(name, number, `table is too tall (${tableRows} rows)`);
  });

  for (const match of source.matchAll(/!\[[^\]]*\]\(([^)]+)\)/g)) {
    const target = match[1].split(/\s+/)[0];
    if (/^(https?:|data:)/.test(target)) continue;
    const resolved = resolve(dirname(path), target);
    if (!existsSync(resolved)) fail(name, 0, `missing asset ${target}`);
  }
}

if (failures > 0) {
  console.error(`Source QA failed with ${failures} issue(s).`);
  process.exit(1);
}
console.log("Source QA passed.");
