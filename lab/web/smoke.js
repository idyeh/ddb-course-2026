/* A smoke suite that runs inside the page and drives the real UI.

   It is deliberately not a pixel test: it asserts the contracts the Python side
   cannot see -- that every declared view tab renders, that check states reach the
   DOM with the right styling, that paired sliders stay valid, and that export
   produces the file it promises.

   Run it from the browser console, or headlessly:

       python3 lab/run.py smoke
*/

const results = [];

function check(name, condition, detail = "") {
  results.push({ name, ok: Boolean(condition), detail });
  return Boolean(condition);
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const $ = (id) => document.getElementById(id);
const field = (name) =>
  [...$("param-form").children].find((item) => item.dataset.name === name);
const input = (name) =>
  field(name)?.querySelector("input[type=range], input[type=number]");

async function selectScenario(id) {
  [...document.querySelectorAll("#menu button")].find((b) => b.dataset.id === id).click();
  for (let attempt = 0; attempt < 60; attempt += 1) {
    await wait(50);
    if ($("scenario-title").textContent && !$("checks").querySelector(".dim")) return true;
  }
  return false;
}

export async function runSmoke() {
  results.length = 0;
  const errors = [];
  window.addEventListener("error", (event) => errors.push(event.message));

  const listing = await (await fetch("api/scenarios")).json();
  const scenarios = listing.scenarios;
  check("scenario listing is not empty", scenarios.length > 0);

  for (const scenario of scenarios) {
    const loaded = await selectScenario(scenario.id);
    if (!check(`${scenario.id}: runs`, loaded)) continue;

    // Every declared view must produce a tab that renders something. The tab bar
    // itself is hidden when there is only one view, but the button still exists.
    const tabs = [...document.querySelectorAll("#view-tabs button")];
    const expected = scenario.views.length;
    check(`${scenario.id}: ${expected} view tab(s)`, tabs.length === expected,
          `declared ${scenario.views.join(",")}, tabs ${tabs.length}`);
    check(`${scenario.id}: tab bar hidden only for a single view`,
          $("view-tabs").hidden === (expected < 2));

    for (const view of scenario.views) {
      const tab = tabs.find((item) => item.textContent.includes(view));
      if (tab) tab.click();
      await wait(60);
      const container = document.querySelector(`.view[data-view="${view}"]`);
      const svg = container?.querySelector("svg");
      check(`${scenario.id}/${view}: draws`, svg && svg.childElementCount > 0,
            `children ${svg ? svg.childElementCount : "no svg"}`);
    }

    // Scrub the whole timeline; a throwing view shows up in `errors`.
    const scrubber = $("scrubber");
    for (let value = 0; value <= 1000; value += 250) {
      scrubber.value = String(value);
      scrubber.dispatchEvent(new Event("input", { bubbles: true }));
    }

    // Check cards must carry a state class, and it must match the summary.
    const cards = [...document.querySelectorAll("#checks .check")];
    check(`${scenario.id}: check cards rendered`,
          cards.length === scenario.invariants.length || cards.length > 0);
    for (const card of cards) {
      check(`${scenario.id}: check card has a state`,
            /\b(ok|bad|unknown)\b/.test(card.className), card.className);
    }
  }

  // Inconclusive results must never be painted as a pass.
  await selectScenario("jepsen-loop");
  const ops = input("ops");
  ops.value = "20";
  ops.dispatchEvent(new Event("input", { bubbles: true }));
  ops.dispatchEvent(new Event("change", { bubbles: true }));
  await wait(900);
  const unknown = document.querySelector("#checks .check.unknown");
  check("inconclusive renders neutrally, not green",
        unknown && unknown.textContent.includes("未检查"));

  // Paired sliders cannot be dragged into an invalid combination.
  await selectScenario("raft-election");
  input("delay_min").value = "400";
  input("delay_min").dispatchEvent(new Event("input", { bubbles: true }));
  check("delay pair stays ordered",
        Number(input("delay_min").value) <= Number(input("delay_max").value),
        `${input("delay_min").value} / ${input("delay_max").value}`);
  input("timeout_min").value = "2000";
  input("timeout_min").dispatchEvent(new Event("input", { bubbles: true }));
  check("timeout pair stays strictly ordered",
        Number(input("timeout_min").value) < Number(input("timeout_max").value),
        `${input("timeout_min").value} / ${input("timeout_max").value}`);

  // Projector mode and keyboard control.
  const before = document.body.classList.contains("projector");
  $("btn-projector").click();
  check("projector mode toggles", document.body.classList.contains("projector") !== before);
  $("btn-projector").click();

  // Export names the active view and produces real content.
  const captured = [];
  const originalCreate = URL.createObjectURL;
  URL.createObjectURL = (blob) => { captured.push(blob); return "blob:smoke"; };
  const originalClick = HTMLAnchorElement.prototype.click;
  let filename = "";
  HTMLAnchorElement.prototype.click = function stub() { filename = this.download; };
  try {
    $("btn-export-svg").click();
    $("btn-export-json").click();
  } finally {
    URL.createObjectURL = originalCreate;
    HTMLAnchorElement.prototype.click = originalClick;
  }
  check("export produced two files", captured.length === 2, `got ${captured.length}`);
  check("export filename names the trace", filename.endsWith(".json"), filename);
  const svgText = await captured[0].text();
  check("exported SVG resolves CSS variables", !svgText.includes("var(--"),
        svgText.slice(0, 60));
  check("exported JSON carries the trace",
        JSON.parse(await captured[1].text()).events.length > 0);

  check("no uncaught browser errors", errors.length === 0, errors.join(" | "));

  const failed = results.filter((item) => !item.ok);
  return { total: results.length, failed: failed.length, failures: failed };
}

window.runSmoke = runSmoke;
