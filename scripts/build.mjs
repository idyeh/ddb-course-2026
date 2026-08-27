import { mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const slidesDir = join(root, "slides");
const outDir = join(root, "dist");
const theme = join(root, "theme", "distributed-db.css");
const marp = join(root, "node_modules", ".bin", process.platform === "win32" ? "marp.cmd" : "marp");

mkdirSync(outDir, { recursive: true });

const decks = readdirSync(slidesDir)
  .filter((name) => name.endsWith(".md"))
  .sort();

if (decks.length === 0) {
  console.error("No Markdown decks found in slides/.");
  process.exit(1);
}

for (const deck of decks) {
  const input = join(slidesDir, deck);
  const output = join(outDir, `${basename(deck, ".md")}.html`);
  const result = spawnSync(marp, [
    input,
    "--output", output,
    "--theme-set", theme,
    "--html",
    "--allow-local-files"
  ], { cwd: root, stdio: "inherit" });

  if (result.status !== 0) process.exit(result.status ?? 1);
}

const cards = decks.map((deck) => {
  const source = readFileSync(join(slidesDir, deck), "utf8");
  const title = source.match(/^title:\s*(.+)$/m)?.[1] ?? basename(deck, ".md");
  const href = `${basename(deck, ".md")}.html`;
  return `<a class="card" href="${href}"><span>${title}</span><small>打开演示 →</small></a>`;
}).join("\n");

writeFileSync(join(outDir, "index.html"), `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>分布式数据库开发</title>
  <style>
    :root{color-scheme:light;--ink:#17202a;--paper:#fbfaf7;--accent:#0b6b6b;--soft:#eaf3f1}
    *{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:"PingFang SC","Microsoft YaHei",sans-serif}
    main{max-width:1040px;margin:auto;padding:64px 32px 80px}h1{font-size:48px;margin:0 0 8px}p{color:#5f6b76;font-size:20px;margin:0 0 40px}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:18px}.card{min-height:130px;padding:24px;border:1px solid #d8ddd9;border-radius:12px;background:white;color:var(--ink);display:flex;flex-direction:column;justify-content:space-between;text-decoration:none;box-shadow:0 8px 24px rgba(23,32,42,.06)}
    .card:hover{border-color:var(--accent);background:var(--soft)}.card span{font-size:23px;font-weight:750}.card small{color:var(--accent);font-size:16px}
  </style>
</head>
<body><main><h1>分布式数据库开发</h1><p>课堂演示课件 · Hong</p><div class="grid">${cards}</div></main></body>
</html>\n`, "utf8");

console.log(`Built ${decks.length} HTML decks and dist/index.html.`);
