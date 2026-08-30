/* Jepsen-style client history: one lane per process, one bar per operation.

   The x axis is event time, not wall time: every call and return becomes a slot of
   equal width. Real ordering and overlap are preserved exactly, but a 15-tick
   operation inside a 2600-tick run is still wide enough to read. */

const NS = "http://www.w3.org/2000/svg";
const LEFT = 88;
const RIGHT = 24;
const TOP = 62;
const LANE = 96;
const BAR = 56;
const MIN_HEIGHT = 236;

const STATUS = {
  ok: { fill: "color-mix(in srgb, var(--accent) 16%, transparent)", stroke: "var(--accent)", label: "ok" },
  info: { fill: "color-mix(in srgb, var(--muted) 14%, transparent)", stroke: "var(--muted)", label: "unknown" },
  fail: { fill: "color-mix(in srgb, var(--accent-2) 14%, transparent)", stroke: "var(--accent-2)", label: "fail" },
  pending: { fill: "transparent", stroke: "var(--line)", label: "进行中" },
};

function el(name, attrs = {}, text = "") {
  const node = document.createElementNS(NS, name);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text !== "") node.textContent = text;
  return node;
}

export class HistoryView {
  constructor(svg) {
    this.svg = svg;
    this.highlighted = new Set();
  }

  setTrace(trace) {
    this.trace = trace;
    this.ops = collect(trace);
    this.faults = trace.events
      .filter((event) => event.type === "fault")
      .map((event) => ({ t: event.t, kind: event.kind, groups: event.groups }));
    this.lanes = [...new Set(this.ops.map((op) => op.client))];
    if (!this.lanes.length) this.lanes = ["客户端"];

    const stops = new Set([0, trace.duration]);
    for (const op of this.ops) { stops.add(op.call); stops.add(op.ret); }
    for (const fault of this.faults) stops.add(fault.t);
    this.stops = [...stops].sort((a, b) => a - b);
    this.width = 960;
    this.height = Math.max(MIN_HEIGHT, TOP + this.lanes.length * LANE + 24);
    this.svg.setAttribute("viewBox", `0 0 ${this.width} ${this.height}`);
    this.highlighted = new Set();
  }

  highlight(opIds) {
    this.highlighted = new Set(opIds || []);
  }

  /** Event-time axis: equal width between consecutive recorded instants. */
  x(t) {
    const stops = this.stops;
    const span = this.width - LEFT - RIGHT;
    if (stops.length < 2) return LEFT;
    if (t <= stops[0]) return LEFT;
    if (t >= stops[stops.length - 1]) return LEFT + span;
    let index = 0;
    while (index < stops.length - 1 && stops[index + 1] < t) index += 1;
    const slot = span / (stops.length - 1);
    const lower = stops[index];
    const upper = stops[index + 1];
    const inner = upper === lower ? 0 : (t - lower) / (upper - lower);
    return LEFT + slot * (index + inner);
  }

  render(state) {
    this.svg.replaceChildren();
    const now = state.t;

    this.svg.appendChild(this._faults(now));
    this.svg.appendChild(this._lanes());
    this.svg.appendChild(this._ops(now));
    this.svg.appendChild(this._playhead(now));
    this.svg.appendChild(this._legend());
  }

  _faults(now) {
    const group = el("g");
    let openAt = null;
    for (const fault of this.faults) {
      if (fault.kind === "partition") openAt = fault.t;
      if (fault.kind === "heal" && openAt !== null) {
        group.appendChild(this._band(openAt, Math.min(fault.t, now), "分区期间"));
        openAt = null;
      }
    }
    if (openAt !== null && now > openAt) this._bandInto(group, openAt, now, "分区期间");
    for (const fault of this.faults) {
      if (fault.t > now) continue;
      const x = this.x(fault.t);
      group.appendChild(el("line", {
        x1: x, y1: TOP - 18, x2: x, y2: this.height - 14,
        stroke: "var(--accent-2)", "stroke-width": 1.2, "stroke-dasharray": "4 4", opacity: .8,
      }));
      group.appendChild(el("text", {
        x: x + 5, y: TOP - 24, "font-size": 11, fill: "var(--accent-2)",
      }, fault.kind === "partition" ? "网络分区" : "网络恢复"));
    }
    return group;
  }

  _band(from, to, label) {
    const group = el("g");
    this._bandInto(group, from, to, label);
    return group;
  }

  _bandInto(group, from, to, label) {
    if (to <= from) return;
    const x = this.x(from);
    group.appendChild(el("rect", {
      x, y: TOP - 14, width: Math.max(0, this.x(to) - x), height: this.height - TOP - 2,
      fill: "color-mix(in srgb, var(--accent-2) 7%, transparent)",
    }));
    group.appendChild(el("text", {
      x: x + 8, y: this.height - 4, "font-size": 10, fill: "var(--accent-2)", opacity: .75,
    }, label));
  }

  _lanes() {
    const group = el("g");
    this.lanes.forEach((lane, index) => {
      const y = TOP + index * LANE + BAR / 2;
      group.appendChild(el("text", {
        x: LEFT - 12, y: y + 4, "text-anchor": "end", "font-size": 13, fill: "var(--muted)",
      }, lane));
      group.appendChild(el("line", {
        x1: LEFT, y1: y, x2: this.width - RIGHT, y2: y,
        stroke: "var(--line)", "stroke-width": 1, opacity: .6,
      }));
    });
    return group;
  }

  _ops(now) {
    const group = el("g");
    for (const op of this.ops) {
      if (op.call > now) continue;
      const lane = Math.max(0, this.lanes.indexOf(op.client));
      const y = TOP + lane * LANE;
      const done = op.ret <= now;
      const style = STATUS[done ? op.status : "pending"] || STATUS.pending;
      const x0 = this.x(op.call);
      const x1 = this.x(Math.min(op.ret, now));
      const width = Math.max(6, x1 - x0);
      const flagged = this.highlighted.has(op.opId);

      const bar = el("rect", {
        x: x0, y, width, height: BAR, rx: 8,
        fill: style.fill, stroke: flagged ? "var(--accent-2)" : style.stroke,
        "stroke-width": flagged ? 2.4 : 1.4,
        "stroke-dasharray": done && op.status === "info" ? "5 3" : "none",
      });
      bar.appendChild(el("title", {},
        `${op.opId} ${op.label}\n调用 t=${op.call}，返回 t=${op.ret}，${style.label}` +
        (op.servedBy ? `\n应答方 ${op.servedBy}` : "")));
      group.appendChild(bar);

      group.appendChild(el("text", {
        x: x0 + 10, y: y + 22, "font-size": 13, "font-weight": 650, fill: "var(--ink)",
      }, op.label));
      group.appendChild(el("text", {
        x: x0 + 10, y: y + 40, "font-size": 12, "font-family": "var(--mono)",
        fill: style.stroke,
      }, done ? resultText(op) : "…"));
      if (flagged) {
        group.appendChild(el("text", {
          x: x0 + width / 2, y: y - 7, "text-anchor": "middle", "font-size": 11,
          fill: "var(--accent-2)", "font-weight": 700,
        }, "反例"));
      }
    }
    return group;
  }

  _playhead(now) {
    const x = this.x(now);
    const group = el("g");
    group.appendChild(el("line", {
      x1: x, y1: TOP - 30, x2: x, y2: this.height - 14,
      stroke: "var(--accent)", "stroke-width": 1.5, opacity: .55,
    }));
    group.appendChild(el("text", {
      x: x + 5, y: TOP - 38, "font-size": 10, "font-family": "var(--mono)", fill: "var(--accent)",
    }, `t = ${Math.round(now)}`));
    return group;
  }

  _legend() {
    const group = el("g", { transform: `translate(${LEFT}, 22)` });
    [["ok", "已确认"], ["info", "unknown（超时，结果未知）"], ["fail", "明确失败"]]
      .forEach(([key, text], index) => {
        const x = index * 210;
        const style = STATUS[key];
        group.appendChild(el("rect", {
          x, y: -9, width: 16, height: 12, rx: 4, fill: style.fill, stroke: style.stroke,
          "stroke-dasharray": key === "info" ? "4 3" : "none",
        }));
        group.appendChild(el("text", {
          x: x + 23, y: 1, "font-size": 11, fill: "var(--muted)",
        }, text));
      });
    return group;
  }
}

function resultText(op) {
  if (op.status === "info") return "unknown";
  if (op.status === "fail") return op.reason ? `fail · ${op.reason}` : "fail";
  if (op.kind === "read") return `→ ${format(op.result)}`;
  return "ok";
}

function format(value) {
  return value === null || value === undefined ? "空" : String(value);
}

function collect(trace) {
  const calls = new Map();
  const ops = [];
  for (const event of trace.events) {
    if (event.type === "client.call") {
      calls.set(event.reqId, event);
    } else if (event.type === "client.return") {
      const call = calls.get(event.reqId);
      if (!call) continue;
      ops.push({
        opId: event.opId, reqId: event.reqId, client: call.client, label: call.op,
        kind: call.kind || "set", call: call.t, ret: event.t, status: event.status,
        result: event.result, reason: event.reason, servedBy: event.served_by,
      });
    }
  }
  return ops;
}
