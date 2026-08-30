/* Key routing and per-shard load. The bars accumulate as the playhead advances,
   so the skew builds up in front of the class instead of arriving as a summary. */

const NS = "http://www.w3.org/2000/svg";
const WIDTH = 960;
const HEIGHT = 452;
const ROUTER = { x: 34, y: 46, w: 128, h: 56 };
const ROW_Y = 46;
const BOX_H = 56;
const BARS_TOP = 150;
const BARS_BOTTOM = 408;

function el(name, attrs = {}, text = "") {
  const node = document.createElementNS(NS, name);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text !== "") node.textContent = text;
  return node;
}

export class ShardsView {
  constructor(svg) {
    this.svg = svg;
  }

  setTrace(trace) {
    this.trace = trace;
    this.shards = trace.nodes.filter((node) => node.kind === "shard").map((node) => node.id);
    if (!this.shards.length) this.shards = trace.nodes.map((node) => node.id);
    this.routes = trace.events.filter((event) => event.type === "shard.route");
    this.splits = trace.events.filter((event) => event.type === "shard.split");
    this.threshold = trace.facts?.balance_threshold || 0;
    const span = WIDTH - 220;
    this.slot = span / Math.max(1, this.shards.length);
    this.boxWidth = Math.min(120, this.slot - 14);
    this.svg.setAttribute("viewBox", `0 0 ${WIDTH} ${HEIGHT}`);
  }

  centerOf(shard) {
    const index = this.shards.indexOf(shard);
    return 208 + this.slot * (index + 0.5);
  }

  render(state) {
    this.svg.replaceChildren();
    const now = state.t;

    const counts = {};
    const stale = new Set();
    let misrouted = 0;
    for (const route of this.routes) {
      if ((route.arriveAt ?? route.t) > now) continue;
      counts[route.shard] = (counts[route.shard] || 0) + 1;
      if (route.stale) { stale.add(route.shard); misrouted += 1; }
    }
    const moved = new Set();
    for (const split of this.splits) if (split.t <= now) moved.add(split.to);

    this.svg.appendChild(this._router(now));
    this.svg.appendChild(this._boxes(counts, stale, moved));
    this.svg.appendChild(this._bars(counts));
    this.svg.appendChild(this._flights(now));
    if (misrouted) this.svg.appendChild(this._warning(misrouted));
  }

  _router(now) {
    const group = el("g");
    group.appendChild(el("rect", {
      x: ROUTER.x, y: ROUTER.y, width: ROUTER.w, height: ROUTER.h, rx: 13,
      fill: "var(--surface)", stroke: "var(--line)", "stroke-width": 2,
    }));
    group.appendChild(el("text", {
      x: ROUTER.x + 14, y: ROUTER.y + 24, "font-size": 13, "font-weight": 700, fill: "var(--ink)",
    }, "路由 router"));
    const sent = this.routes.filter((route) => route.t <= now).length;
    group.appendChild(el("text", {
      x: ROUTER.x + 14, y: ROUTER.y + 43, "font-size": 11, "font-family": "var(--mono)",
      fill: "var(--muted)",
    }, `${sent} / ${this.routes.length} 请求`));
    return group;
  }

  _boxes(counts, stale, moved) {
    const group = el("g");
    for (const shard of this.shards) {
      const center = this.centerOf(shard);
      const x = center - this.boxWidth / 2;
      const flagged = stale.has(shard);
      group.appendChild(el("rect", {
        x, y: ROW_Y, width: this.boxWidth, height: BOX_H, rx: 12,
        fill: moved.has(shard) ? "color-mix(in srgb, var(--accent) 12%, transparent)" : "var(--surface)",
        stroke: flagged ? "var(--accent-2)" : "var(--line)",
        "stroke-width": flagged ? 2.5 : 2,
      }));
      group.appendChild(el("text", {
        x: center, y: ROW_Y + 25, "text-anchor": "middle", "font-size": 15, "font-weight": 700,
        fill: flagged ? "var(--accent-2)" : "var(--ink)",
      }, shard));
      group.appendChild(el("text", {
        x: center, y: ROW_Y + 43, "text-anchor": "middle", "font-size": 11,
        "font-family": "var(--mono)", fill: "var(--muted)",
      }, `${counts[shard] || 0}`));
      if (moved.has(shard)) {
        group.appendChild(el("text", {
          x: center, y: ROW_Y - 8, "text-anchor": "middle", "font-size": 10,
          fill: "var(--accent)",
        }, "迁入 migrated in"));
      }
    }
    return group;
  }

  _bars(counts) {
    const group = el("g");
    const values = this.shards.map((shard) => counts[shard] || 0);
    const total = values.reduce((sum, value) => sum + value, 0);
    const peak = Math.max(1, ...values);
    const average = total / Math.max(1, this.shards.length);
    const height = BARS_BOTTOM - BARS_TOP;

    group.appendChild(el("line", {
      x1: 190, y1: BARS_BOTTOM, x2: WIDTH - 20, y2: BARS_BOTTOM,
      stroke: "var(--line)", "stroke-width": 1.5,
    }));
    group.appendChild(el("text", {
      x: 34, y: BARS_TOP - 8, "font-size": 12, "font-weight": 650, fill: "var(--ink)",
    }, "各分片累计负载"));

    if (this.threshold && total) {
      const limit = average * this.threshold;
      const y = BARS_BOTTOM - (limit / peak) * height;
      if (y > BARS_TOP - 20) {
        group.appendChild(el("line", {
          x1: 190, y1: y, x2: WIDTH - 20, y2: y, stroke: "var(--accent-2)",
          "stroke-width": 1.2, "stroke-dasharray": "6 5", opacity: .8,
        }));
        group.appendChild(el("text", {
          x: 190, y: y - 6, "font-size": 10, fill: "var(--accent-2)",
        }, `均衡阈值 ${this.threshold}× 平均`));
      }
    }

    this.shards.forEach((shard, index) => {
      const value = values[index];
      const barHeight = (value / peak) * height;
      const center = this.centerOf(shard);
      const hot = value > average * (this.threshold || 99);
      group.appendChild(el("rect", {
        x: center - this.boxWidth / 2, y: BARS_BOTTOM - barHeight,
        width: this.boxWidth, height: Math.max(0, barHeight), rx: 6,
        fill: hot ? "color-mix(in srgb, var(--accent-2) 30%, transparent)"
                  : "color-mix(in srgb, var(--accent) 22%, transparent)",
        stroke: hot ? "var(--accent-2)" : "var(--accent)", "stroke-width": 1.2,
      }));
      if (value) {
        group.appendChild(el("text", {
          x: center, y: BARS_BOTTOM - barHeight - 7, "text-anchor": "middle",
          "font-size": 11, "font-family": "var(--mono)",
          fill: hot ? "var(--accent-2)" : "var(--accent)",
        }, total ? `${Math.round((value / total) * 100)}%` : ""));
      }
    });
    return group;
  }

  _flights(now) {
    const group = el("g");
    for (const route of this.routes) {
      const arrive = route.arriveAt ?? route.t;
      if (route.t > now || arrive <= now) continue;
      const progress = (now - route.t) / Math.max(1, arrive - route.t);
      const x0 = ROUTER.x + ROUTER.w;
      const y0 = ROUTER.y + ROUTER.h / 2;
      const x1 = this.centerOf(route.shard);
      const y1 = ROW_Y + BOX_H;
      group.appendChild(el("circle", {
        cx: x0 + (x1 - x0) * progress, cy: y0 + (y1 - y0) * progress, r: 5,
        fill: route.hot ? "var(--accent-2)" : "var(--accent)", opacity: .9,
      }));
    }
    return group;
  }

  _warning(count) {
    return el("text", {
      x: 34, y: HEIGHT - 12, "font-size": 11, fill: "var(--accent-2)",
    }, `按过期路由送达的请求：${count}`);
  }
}
