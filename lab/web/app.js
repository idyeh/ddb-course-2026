/* Menu, parameter form, and the wiring between the API and the player. */

import { Player } from "./player.js";
import { ClusterView } from "./views/cluster.js";
import { HistoryView } from "./views/history.js";
import { TimelineView } from "./views/timeline.js";
import { ShardsView } from "./views/shards.js";
import { StorageView } from "./views/storage.js";
import { DataflowView } from "./views/dataflow.js";

const $ = (id) => document.getElementById(id);

const SIGNIFICANT = new Set([
  "note", "fault", "node.crash", "node.restart", "log.append", "log.commit",
  "log.truncate", "client.call", "client.return", "client.redirect", "msg.drop",
]);

const EVENT_LABEL = {
  note: "要点", "msg.send": "发送", "msg.deliver": "送达", "msg.drop": "丢弃",
  "node.state": "状态", "node.crash": "崩溃", "node.restart": "重启",
  "log.append": "追加", "log.commit": "提交", "log.truncate": "截断",
  "state.apply": "应用", fault: "故障", "client.call": "调用",
  "client.return": "返回", "client.redirect": "重定向",
};

const STATUS_TEXT = { ok: "ok", info: "unknown（超时，结果未知）", fail: "fail" };

const app = {
  scenarios: [],
  current: null,
  trace: null,
  showAllEvents: false,
  eventCursor: 0,
};

const VIEW_LABEL = { cluster: "集群 cluster", history: "客户端历史 history",
                     timeline: "时空图 timeline", shards: "分片 shards",
                     storage: "日志与恢复 storage",
                     dataflow: "算子与数据流 dataflow" };
const views = {
  cluster: new ClusterView($("stage-svg"), $("log-matrix")),
  history: new HistoryView($("history-svg")),
  timeline: new TimelineView($("timeline-svg")),
  shards: new ShardsView($("shards-svg")),
  storage: new StorageView($("storage-svg")),
  dataflow: new DataflowView($("dataflow-svg")),
};
app.activeView = "cluster";

const player = new Player({
  onRender: (snapshot) => {
    views[app.activeView]?.render(snapshot);
    $("scrubber").value = String(Math.round((snapshot.t / snapshot.duration) * 1000));
    $("tick-readout").textContent = `t = ${Math.round(snapshot.t)} / ${snapshot.duration}`;
    $("btn-play").textContent = player.playing ? "❚❚" : "▶";
    renderNotes(snapshot);
    renderEvents(snapshot);
    renderPartitionBadge(snapshot);
  },
});

/* -- boot ------------------------------------------------------------------ */

async function init() {
  const response = await fetch("api/scenarios");
  const payload = await response.json();
  app.scenarios = payload.scenarios;
  renderMenu();
  wireControls();
  if (app.scenarios.length) select(app.scenarios[0].id);
}

function renderMenu() {
  const menu = $("menu");
  menu.replaceChildren();
  let lecture = null;
  app.scenarios.forEach((scenario, index) => {
    if (scenario.lecture !== lecture) {
      lecture = scenario.lecture;
      const heading = document.createElement("div");
      heading.className = "lecture";
      heading.textContent = lecture;
      menu.appendChild(heading);
    }
    const button = document.createElement("button");
    button.dataset.id = scenario.id;
    button.innerHTML = `<b>${index + 1}. ${scenario.title}</b><i>${scenario.subtitle}</i>`;
    button.addEventListener("click", () => select(scenario.id));
    menu.appendChild(button);
  });
}

function select(id) {
  const scenario = app.scenarios.find((item) => item.id === id);
  if (!scenario) return;
  app.current = scenario;
  for (const button of $("menu").querySelectorAll("button")) {
    button.setAttribute("aria-current", String(button.dataset.id === id));
  }
  $("scenario-title").textContent = scenario.title;
  $("scenario-subtitle").textContent = scenario.question || scenario.subtitle;
  $("scenario-caveat").textContent = scenario.caveat;
  $("scenario-caveat").hidden = !scenario.caveat;
  $("takeaway").textContent = scenario.takeaway;
  $("takeaway-card").hidden = !scenario.takeaway;
  renderPrediction(scenario);
  buildForm(scenario);
  run();
}

/* -- parameter form -------------------------------------------------------- */

function buildForm(scenario) {
  const form = $("param-form");
  form.replaceChildren();
  for (const param of scenario.params) {
    form.appendChild(field(param));
  }
}

function field(param) {
  const wrapper = document.createElement("div");
  wrapper.className = "field";
  wrapper.dataset.name = param.name;
  wrapper.dataset.kind = param.kind;

  const label = document.createElement("label");
  label.innerHTML = `<span>${param.label}${param.axis ? ' <em class="axis-tag">对比轴</em>' : ""}</span>`;
  const readout = document.createElement("span");
  readout.className = "value";
  label.appendChild(readout);
  wrapper.appendChild(label);

  if (param.kind === "choice") {
    if (param.choices.length <= 3) {
      const pills = document.createElement("div");
      pills.className = "pills";
      for (const choice of param.choices) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = choice.label;
        button.dataset.value = choice.value;
        button.setAttribute("aria-pressed", String(choice.value === param.default));
        button.addEventListener("click", () => {
          for (const sibling of pills.children) sibling.setAttribute("aria-pressed", "false");
          button.setAttribute("aria-pressed", "true");
          wrapper.dataset.value = choice.value;
          run();
        });
        pills.appendChild(button);
      }
      wrapper.dataset.value = param.default;
      wrapper.appendChild(pills);
    } else {
      const select = document.createElement("select");
      for (const choice of param.choices) {
        const option = document.createElement("option");
        option.value = choice.value;
        option.textContent = choice.label;
        select.appendChild(option);
      }
      select.value = param.default;
      select.addEventListener("change", run);
      wrapper.appendChild(select);
    }
  } else if (param.name === "seed") {
    const row = document.createElement("div");
    row.className = "seed-row";
    const input = document.createElement("input");
    input.type = "number";
    input.value = param.default;
    input.min = param.min ?? 0;
    input.addEventListener("change", run);
    const dice = document.createElement("button");
    dice.type = "button";
    dice.className = "ghost small";
    dice.textContent = "随机";
    dice.title = "换一个种子，探索别的调度";
    dice.addEventListener("click", () => {
      input.value = String(Math.floor(Math.random() * 100000));
      run();
    });
    row.append(input, dice);
    wrapper.appendChild(row);
  } else if (param.min !== null && param.max !== null) {
    const input = document.createElement("input");
    input.type = "range";
    input.min = param.min;
    input.max = param.max;
    input.step = param.step ?? (param.kind === "float" ? 0.01 : 1);
    input.value = param.default;
    readout.textContent = param.default;
    input.addEventListener("input", () => {
      readout.textContent = input.value;
      enforceRules(param.name);
    });
    input.addEventListener("change", run);
    wrapper.appendChild(input);
  } else {
    const input = document.createElement("input");
    input.type = "number";
    input.value = param.default;
    input.addEventListener("change", run);
    wrapper.appendChild(input);
  }

  if (param.help) {
    const help = document.createElement("p");
    help.className = "help";
    help.textContent = param.help;
    wrapper.appendChild(help);
  }
  return wrapper;
}

/** Keep paired fields consistent while dragging, so an invalid run cannot be built. */
function enforceRules(changed) {
  for (const rule of app.current?.rules || []) {
    const lower = fieldInput(rule.lower);
    const upper = fieldInput(rule.upper);
    if (!lower || !upper) continue;
    const gap = rule.strict ? Number(lower.step || 1) : 0;
    if (Number(lower.value) + gap > Number(upper.value)) {
      const follower = changed === rule.lower ? upper : lower;
      const leaderValue = changed === rule.lower
        ? Number(lower.value) + gap : Number(upper.value) - gap;
      follower.value = String(Math.min(Number(follower.max ?? leaderValue),
                                       Math.max(Number(follower.min ?? leaderValue),
                                                leaderValue)));
      const readout = follower.closest(".field")?.querySelector(".value");
      if (readout) readout.textContent = follower.value;
    }
  }
}

function fieldInput(name) {
  const wrapper = [...$("param-form").children].find((item) => item.dataset.name === name);
  return wrapper ? wrapper.querySelector("input[type=range], input[type=number]") : null;
}

function readParams() {
  const params = {};
  for (const wrapper of $("param-form").children) {
    const name = wrapper.dataset.name;
    if (wrapper.dataset.value !== undefined) {
      params[name] = wrapper.dataset.value;
      continue;
    }
    const input = wrapper.querySelector("input, select");
    if (input) params[name] = input.value;
  }
  return params;
}

/* -- running --------------------------------------------------------------- */

let inFlight = null;

async function run() {
  if (!app.current) return;
  const payload = { scenario: app.current.id, params: readParams() };
  const token = Symbol("run");
  inFlight = token;
  try {
    const response = await fetch("api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const trace = await response.json();
    if (inFlight !== token) return;
    if (trace.error) { showNotice(trace.error, "bad"); return; }
    app.trace = trace;
    app.eventCursor = 0;
    $("badge-seed").textContent = `seed ${trace.seed}`;
    for (const instance of Object.values(views)) instance.setTrace(trace);
    renderViewTabs(trace.views || ["cluster"]);
    highlightCounterexample(trace.summary.checks || []);
    player.load(trace);
    renderChecks(trace.summary.checks || []);
    renderMetrics(trace.summary.metrics || {});
    showNotice((trace.summary.advisories || []).join(" "), "warn");
    $("sweep-result").hidden = true;
  } catch (error) {
    showNotice(`运行失败：${error.message}`, "bad");
  }
}

function renderViewTabs(names) {
  const host = $("view-tabs");
  host.replaceChildren();
  host.hidden = names.length < 2;
  if (!names.includes(app.activeView)) app.activeView = names[0];
  for (const name of names) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = VIEW_LABEL[name] || name;
    button.setAttribute("aria-pressed", String(name === app.activeView));
    button.addEventListener("click", () => {
      app.activeView = name;
      renderViewTabs(names);
      player.seek(player.t);
    });
    host.appendChild(button);
  }
  for (const [name, container] of Object.entries(viewContainers())) {
    container.hidden = name !== app.activeView;
  }
}

function viewContainers() {
  const map = {};
  for (const node of document.querySelectorAll(".view")) map[node.dataset.view] = node;
  return map;
}

/** Outline the operations a failing checker singled out. */
function highlightCounterexample(checks) {
  const failing = checks.find((check) => (check.status || (check.ok ? "pass" : "fail")) === "fail"
                                        && check.counterexample?.length);
  const ids = failing
    ? failing.counterexample.map((event) => event.opId ?? event.id).filter(Boolean)
    : [];
  for (const instance of Object.values(views)) instance.highlight?.(ids);
}

function renderChecks(checks) {
  const host = $("checks");
  host.replaceChildren();
  if (!checks.length) { host.innerHTML = '<p class="dim">该演示未声明不变式。</p>'; return; }
  const LABEL = { pass: "通过", fail: "违反", inconclusive: "未检查" };
  const CLASS = { pass: "ok", fail: "bad", inconclusive: "unknown" };
  for (const check of checks) {
    const status = check.status || (check.ok ? "pass" : "fail");
    const box = document.createElement("div");
    box.className = `check ${CLASS[status]}`;
    box.innerHTML = `<div class="name"><em>${LABEL[status]}</em>${check.name}</div>
                     <p>${check.detail}</p>`;
    if (status === "fail" && check.counterexample?.length) {
      const jump = document.createElement("button");
      jump.className = "ghost small";
      jump.textContent = "跳到反例";
      jump.style.marginTop = "6px";
      jump.addEventListener("click", () => {
        player.seek(check.counterexample[check.counterexample.length - 1].t);
        player.pause();
      });
      box.appendChild(jump);
    }
    host.appendChild(box);
  }
}

function renderMetrics(metrics) {
  const host = $("metrics");
  host.replaceChildren();
  const entries = Object.entries(metrics);
  if (!entries.length) { host.innerHTML = '<p class="dim">无指标。</p>'; return; }
  for (const [key, value] of entries) {
    const dt = document.createElement("dt");
    dt.textContent = key;
    const dd = document.createElement("dd");
    dd.textContent = String(value);
    host.append(dt, dd);
  }
}

/* -- notes and event log --------------------------------------------------- */

function renderNotes(snapshot) {
  const host = $("note-list");
  const notes = snapshot.notes;
  if (host.children.length > notes.length) host.replaceChildren();
  for (let index = host.children.length; index < notes.length; index += 1) {
    const note = notes[index];
    const box = document.createElement("div");
    box.className = `note ${note.level}`;
    box.innerHTML = `<div class="head"><span>${note.title || "教学要点"}</span><time>t = ${note.t}</time></div>
                     <p>${note.text}</p>`;
    host.appendChild(box);
  }
  for (const child of host.children) child.classList.remove("is-current");
  const last = host.lastElementChild;
  // Scroll the panel itself; scrollIntoView would drag the whole page along.
  if (last) { last.classList.add("is-current"); host.scrollTop = host.scrollHeight; }
}

function renderEvents(snapshot) {
  const host = $("event-list");
  const events = app.trace ? app.trace.events : [];
  if (app.eventCursor > 0 && (app.eventCursor > events.length ||
      events[app.eventCursor - 1].t > snapshot.t)) {
    host.replaceChildren();
    app.eventCursor = 0;
  }
  while (app.eventCursor < events.length && events[app.eventCursor].t <= snapshot.t) {
    const event = events[app.eventCursor];
    app.eventCursor += 1;
    // Partition drops are shown vividly on the stage; in the log they would bury
    // every state change under repeated heartbeat losses.
    const noisy = event.type === "msg.drop" && event.cause === "partition";
    if (!app.showAllEvents && (!SIGNIFICANT.has(event.type) || noisy)) continue;
    const row = document.createElement("div");
    row.className = "event-row"
      + (event.type === "fault" ? " is-fault" : "")
      + (event.type === "msg.drop" ? " is-drop" : "");
    row.innerHTML = `<time>${event.t}</time><span class="kind">${EVENT_LABEL[event.type] || event.type}</span>
                     <span class="body">${describe(event)}</span>`;
    host.appendChild(row);
  }
  host.scrollTop = host.scrollHeight;
}

function describe(event) {
  switch (event.type) {
    case "note": return event.title ? `${event.title}：${event.text}` : event.text;
    case "fault":
      return event.kind === "partition"
        ? `网络分区 → ${event.groups.map((g) => `{${g.join(" ")}}`).join(" | ")}`
        : "网络恢复";
    case "msg.send": return `${event.from} → ${event.to} ${event.kind} ${event.label}`;
    case "msg.deliver": return `${event.id} 送达`;
    case "msg.drop":
      return event.cause === "simulation_end"
        ? `${event.id} 在演示结束时仍在途中`
        : `${event.id} 丢弃（${event.cause}）`;
    case "node.state": return `${event.node} → ${event.state} term ${event.term}（${event.reason}）`;
    case "node.crash": return `${event.node} 崩溃，易失状态丢失，日志与任期保留`;
    case "node.restart": return `${event.node} 重启，term ${event.term}，日志末尾 ${event.lastIndex}`;
    case "log.append": return `${event.node} 索引 ${event.index} ← term ${event.term} ${event.cmd}`;
    case "log.commit": return `${event.node} 提交索引 ${event.index}（term ${event.term} ${event.cmd}）`;
    case "log.truncate": return `${event.node} 从索引 ${event.from} 起截断冲突后缀`;
    case "state.apply": return `${event.node} 应用索引 ${event.index}${event.deduped ? "（去重命中）" : ""} → ${event.state}`;
    case "client.call": return `${event.opId} / ${event.reqId} ${event.op} → ${event.target}`;
    case "client.return": return `${event.opId} / ${event.reqId} → ${STATUS_TEXT[event.status] || event.status}`;
    case "client.redirect": return `${event.opId} 被 ${event.from} 重定向到 ${event.to}`;
    default: return JSON.stringify(event);
  }
}

function renderPartitionBadge(snapshot) {
  const badge = $("badge-partition");
  if (!snapshot.partitions) { badge.hidden = true; return; }
  badge.hidden = false;
  badge.textContent = "分区 " + snapshot.partitions.map((g) => `{${g.join(" ")}}`).join(" | ");
}

/* -- prediction ------------------------------------------------------------ */

function renderPrediction(scenario) {
  const card = $("prediction");
  const prediction = scenario.prediction;
  card.hidden = !prediction;
  if (!prediction) return;
  $("prediction-question").textContent = prediction.question;
  const host = $("prediction-options");
  host.replaceChildren();
  $("prediction-reveal").hidden = true;
  prediction.options.forEach((text, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `${"ABCD"[index]}. ${text}`;
    button.addEventListener("click", () => {
      for (const [position, sibling] of [...host.children].entries()) {
        sibling.classList.toggle("right", position === prediction.answer);
        sibling.classList.toggle("wrong", position === index && index !== prediction.answer);
      }
      $("prediction-reveal").textContent = prediction.why;
      $("prediction-reveal").hidden = false;
    });
    host.appendChild(button);
  });
}

/* -- controls -------------------------------------------------------------- */

function wireControls() {
  $("btn-play").addEventListener("click", () => player.toggle());
  $("btn-prev").addEventListener("click", () => player.stepEvent(-1));
  $("btn-next").addEventListener("click", () => player.stepEvent(1));
  $("scrubber").addEventListener("input", (event) => {
    // Read the slider before pausing: pause() re-renders and rewrites this value.
    const fraction = Number(event.target.value) / 1000;
    player.pause();
    player.seek(fraction * player.duration);
  });
  $("speed").addEventListener("input", (event) => {
    player.speed = Number(event.target.value);
    $("speed-readout").textContent = `${player.speed}×`;
  });
  $("pause-on-note").addEventListener("change", (event) => {
    player.pauseOnNote = event.target.checked;
  });
  $("btn-run").addEventListener("click", run);
  $("btn-reset").addEventListener("click", () => { buildForm(app.current); run(); });
  $("btn-sweep").addEventListener("click", sweep);
  $("btn-projector").addEventListener("click", () => document.body.classList.toggle("projector"));
  $("btn-export-svg").addEventListener("click", exportSvg);
  $("btn-export-json").addEventListener("click", exportJson);

  const toggle = document.createElement("label");
  toggle.className = "switch";
  toggle.style.marginLeft = "auto";
  toggle.innerHTML = '<input type="checkbox"><span></span>全部消息';
  toggle.querySelector("input").addEventListener("change", (event) => {
    app.showAllEvents = event.target.checked;
    $("event-list").replaceChildren();
    app.eventCursor = 0;
    player.seek(player.t);
  });
  document.querySelector(".events h2").appendChild(toggle);

  document.addEventListener("keydown", (event) => {
    if (event.target.matches("input, select, textarea")) return;
    if (event.code === "Space") { event.preventDefault(); player.toggle(); }
    else if (event.code === "ArrowRight") { event.preventDefault(); player.stepEvent(1); }
    else if (event.code === "ArrowLeft") { event.preventDefault(); player.stepEvent(-1); }
    else if (event.key.toLowerCase() === "r") run();
    else if (event.key.toLowerCase() === "p") document.body.classList.toggle("projector");
    else if (/^[1-9]$/.test(event.key)) {
      const scenario = app.scenarios[Number(event.key) - 1];
      if (scenario) select(scenario.id);
    }
  });
}

async function sweep() {
  if (!app.current) return;
  const host = $("sweep-result");
  host.hidden = false;
  host.innerHTML = '<p class="dim">扫描中…</p>';
  const response = await fetch("api/sweep", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      scenario: app.current.id, params: readParams(),
      from: Number(readParams().seed || 2026), count: 20,
    }),
  });
  const report = await response.json();
  if (report.error) { host.innerHTML = `<p class="dim">${report.error}</p>`; return; }
  const rows = report.rows.map((row) => `<tr><td>${row.seed}</td>
      <td class="${row.violations.length ? "bad" : "good"}">${
        row.violations.length ? row.violations.join("；") : "全部通过"}</td></tr>`).join("");
  host.innerHTML = `<p class="dim">扫描 ${report.checked} 个种子，${report.failing} 个出现反例。
    固定种子提供重放，不提供覆盖率。</p><table>${rows}</table>`;
}

/* -- export ---------------------------------------------------------------- */

function download(name, blob) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
  toast(`已导出 ${name}`);
}

function exportSvg() {
  const source = { history: $("history-svg"), timeline: $("timeline-svg"),
                   shards: $("shards-svg"), storage: $("storage-svg"),
                   dataflow: $("dataflow-svg") }[app.activeView] || $("stage-svg");
  const svg = source.cloneNode(true);
  svg.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  const style = getComputedStyle(document.documentElement);
  const [, , boxWidth, boxHeight] = source.getAttribute("viewBox").split(/\s+/);
  const background = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  background.setAttribute("width", boxWidth);
  background.setAttribute("height", boxHeight);
  background.setAttribute("fill", style.getPropertyValue("--paper").trim() || "#fbfaf7");
  svg.insertBefore(background, svg.firstChild);
  let text = new XMLSerializer().serializeToString(svg);
  text = text.replace(/var\(--([a-z0-9-]+)\)/g,
    (match, name) => style.getPropertyValue(`--${name}`).trim() || match);
  download(`${app.current.id}-${app.activeView}-t${Math.round(player.t)}.svg`,
           new Blob([text], { type: "image/svg+xml" }));
}

function exportJson() {
  if (!app.trace) return;
  download(`${app.trace.scenario}-${app.trace.seed}.json`,
           new Blob([JSON.stringify(app.trace, null, 2)], { type: "application/json" }));
}

/** Parameter errors and advisories live next to the form, not in a passing toast. */
function showNotice(message, kind) {
  const box = $("param-notice");
  box.hidden = !message;
  box.textContent = message || "";
  box.className = `notice ${kind || ""}`;
}

let toastTimer = null;
function toast(message) {
  const box = $("toast");
  box.textContent = message;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, 2600);
}

init();
