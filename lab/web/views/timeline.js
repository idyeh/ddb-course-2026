/* Space-time diagram: one lane per process, events as dots, messages as arrows.

   The stamps under each dot come from whichever clock the scenario is currently
   using, so switching the clock parameter re-labels the same execution. */

const NS = "http://www.w3.org/2000/svg";
const LEFT = 132;
const RIGHT = 46;
const TOP = 96;
const LANE = 104;

const CLOCK_LABEL = {
  physical: "物理时钟 physical",
  lamport: "Lamport 逻辑时钟",
  vector: "向量时钟 vector",
  hlc: "混合逻辑时钟 HLC",
};

function el(name, attrs = {}, text = "") {
  const node = document.createElementNS(NS, name);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text !== "") node.textContent = text;
  return node;
}

function stamp(event, clock) {
  if (clock === "vector") return `(${event.vector.join(",")})`;
  if (clock === "hlc") return `(${event.hlc.join(",")})`;
  if (clock === "lamport") return `L=${event.lamport}`;
  return `pt=${event.physical}`;
}

export class TimelineView {
  constructor(svg) {
    this.svg = svg;
    this.highlighted = new Set();
  }

  setTrace(trace) {
    this.trace = trace;
    this.clock = trace.params?.clock || "physical";
    this.processes = trace.facts?.processes || trace.nodes.map((node) => ({ id: node.id, offset: 0 }));
    this.events = trace.events.filter((event) => event.type === "tl.event");
    this.messages = [];
    const open = new Map();
    for (const event of trace.events) {
      if (event.type === "msg.send") open.set(event.id, { ...event, recvAt: event.eta });
      else if (event.type === "msg.deliver" && open.has(event.id)) {
        const message = open.get(event.id);
        this.messages.push({ ...message, recvAt: event.t });
      }
    }
    const ticks = this.events.map((event) => event.t);
    this.span = [Math.min(0, ...ticks), Math.max(...ticks, trace.duration)];
    this.width = 960;
    this.height = TOP + this.processes.length * LANE + 30;
    this.svg.setAttribute("viewBox", `0 0 ${this.width} ${this.height}`);
    this.highlighted = new Set();
  }

  highlight(ids) {
    this.highlighted = new Set(ids || []);
  }

  x(tick) {
    const [low, high] = this.span;
    const span = Math.max(1, high - low);
    return LEFT + ((tick - low) / span) * (this.width - LEFT - RIGHT);
  }

  y(proc) {
    const index = this.processes.findIndex((item) => item.id === proc);
    return TOP + Math.max(0, index) * LANE;
  }

  render(state) {
    this.svg.replaceChildren();
    const now = state.t;

    const defs = el("defs");
    const marker = el("marker", {
      id: "tl-arrow", viewBox: "0 0 10 10", refX: "9", refY: "5",
      markerWidth: "6", markerHeight: "6", orient: "auto-start-reverse",
    });
    marker.appendChild(el("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "var(--muted)" }));
    defs.appendChild(marker);
    this.svg.appendChild(defs);

    this.svg.appendChild(this._header());
    this.svg.appendChild(this._lanes());
    this.svg.appendChild(this._messages(now));
    this.svg.appendChild(this._events(now));
    this.svg.appendChild(this._playhead(now));
  }

  _header() {
    const group = el("g");
    group.appendChild(el("text", {
      x: LEFT, y: 26, "font-size": 13, "font-weight": 650, fill: "var(--ink)",
    }, `事件下方的标注：${CLOCK_LABEL[this.clock] || this.clock}`));
    group.appendChild(el("text", {
      x: LEFT, y: 44, "font-size": 11, fill: "var(--muted)",
    }, "横轴是真实时间；箭头是消息。被圈出的两个事件是检查器给出的反例。"));
    return group;
  }

  _lanes() {
    const group = el("g");
    for (const process of this.processes) {
      const y = this.y(process.id);
      group.appendChild(el("line", {
        x1: LEFT - 18, y1: y, x2: this.width - RIGHT, y2: y,
        stroke: "var(--line)", "stroke-width": 1.5,
      }));
      group.appendChild(el("text", {
        x: LEFT - 30, y: y - 4, "text-anchor": "end", "font-size": 14, "font-weight": 650,
        fill: "var(--ink)",
      }, process.id));
      if (process.offset) {
        group.appendChild(el("text", {
          x: LEFT - 30, y: y + 13, "text-anchor": "end", "font-size": 11,
          "font-family": "var(--mono)", fill: "var(--accent-2)",
        }, `时钟 ${process.offset > 0 ? "+" : ""}${process.offset}`));
      }
    }
    return group;
  }

  _messages(now) {
    const group = el("g");
    for (const message of this.messages) {
      if (message.t > now) continue;
      const progress = Math.min(1, (now - message.t) / Math.max(1, message.recvAt - message.t));
      const x0 = this.x(message.t);
      const y0 = this.y(message.from);
      const x1 = this.x(message.recvAt);
      const y1 = this.y(message.to);
      group.appendChild(el("line", {
        x1: x0, y1: y0, x2: x0 + (x1 - x0) * progress, y2: y0 + (y1 - y0) * progress,
        stroke: "var(--muted)", "stroke-width": 1.6, opacity: .75,
        "marker-end": progress >= 1 ? "url(#tl-arrow)" : "none",
      }));
      if (progress < 1) {
        group.appendChild(el("circle", {
          cx: x0 + (x1 - x0) * progress, cy: y0 + (y1 - y0) * progress, r: 5,
          fill: "var(--accent)",
        }));
      }
    }
    return group;
  }

  _events(now) {
    const group = el("g");
    for (const event of this.events) {
      if (event.t > now) continue;
      const x = this.x(event.t);
      const y = this.y(event.proc);
      const flagged = this.highlighted.has(event.id);
      const color = flagged ? "var(--accent-2)" : "var(--accent)";
      if (flagged) {
        group.appendChild(el("circle", {
          cx: x, cy: y, r: 15, fill: "none", stroke: "var(--accent-2)",
          "stroke-width": 2, "stroke-dasharray": "4 3",
        }));
      }
      group.appendChild(el("circle", {
        cx: x, cy: y, r: 7, fill: "var(--surface)", stroke: color, "stroke-width": 2.5,
      }));
      group.appendChild(el("text", {
        x, y: y - 18, "text-anchor": "middle", "font-size": 13, "font-weight": 650,
        fill: flagged ? "var(--accent-2)" : "var(--ink)",
      }, event.name));
      group.appendChild(el("text", {
        x, y: y + 26, "text-anchor": "middle", "font-size": 12, "font-family": "var(--mono)",
        fill: flagged ? "var(--accent-2)" : "var(--muted)",
      }, stamp(event, this.clock)));
      const detail = el("title", {}, `${event.name} @ t=${event.t}\n` +
        `physical=${event.physical}  lamport=${event.lamport}\n` +
        `vector=(${event.vector.join(",")})  hlc=(${event.hlc.join(",")})`);
      group.appendChild(detail);
    }
    return group;
  }

  _playhead(now) {
    const x = this.x(now);
    const group = el("g");
    group.appendChild(el("line", {
      x1: x, y1: TOP - 34, x2: x, y2: this.height - 16,
      stroke: "var(--accent)", "stroke-width": 1.4, opacity: .5,
    }));
    group.appendChild(el("text", {
      x: x + 5, y: TOP - 38, "font-size": 10, "font-family": "var(--mono)", fill: "var(--accent)",
    }, `t = ${Math.round(now)}`));
    return group;
  }
}
