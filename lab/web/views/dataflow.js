/* Operator DAG with data flowing down the edges.

   Scans at the top, result at the bottom. Edge labels carry rows and bytes,
   because in a distributed plan the bytes explain more than the operator names.
   The widest input in a layer is highlighted: a query waits for its slowest
   partition, not for the average one. */

const NS = "http://www.w3.org/2000/svg";
const WIDTH = 960;
const HEIGHT = 470;
const TOP = 56;
const LAYER_H = 92;
const NODE_H = 46;

const KIND_STYLE = {
  scan: { color: "#0b6b6b", icon: "扫描" },
  filter: { color: "#2f8f5b", icon: "过滤" },
  project: { color: "#2f8f5b", icon: "投影" },
  exchange: { color: "#c14f2f", icon: "交换" },
  join: { color: "#4a6fa5", icon: "连接" },
  aggregate: { color: "#7a5ea8", icon: "聚合" },
  result: { color: "#17202a", icon: "结果" },
};

function el(name, attrs = {}, text = "") {
  const node = document.createElementNS(NS, name);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text !== "") node.textContent = text;
  return node;
}

function bytes(value) {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)} MB`;
  if (value >= 1000) return `${(value / 1000).toFixed(1)} KB`;
  return `${value} B`;
}

export class DataflowView {
  constructor(svg) {
    this.svg = svg;
  }

  setTrace(trace) {
    this.trace = trace;
    const plan = trace.facts?.plan || { nodes: [], edges: [] };
    this.nodes = plan.nodes;
    this.edges = plan.edges;
    this.stages = trace.events.filter((event) => event.type === "flow.stage");

    const layers = new Map();
    for (const node of this.nodes) {
      if (!layers.has(node.layer)) layers.set(node.layer, []);
      layers.get(node.layer).push(node);
    }
    this.layers = layers;
    this.position = {};
    this.peak = {};
    for (const [layer, members] of layers) {
      const width = Math.min(184, (WIDTH - 90) / members.length - 14);
      const span = members.length * (width + 14) - 14;
      const start = (WIDTH - span) / 2;
      this.peak[layer] = Math.max(...members.map((node) => node.inputs ?? node.rows ?? 0));
      members.forEach((node, index) => {
        this.position[node.id] = {
          x: start + index * (width + 14), y: TOP + layer * LAYER_H,
          w: width, h: NODE_H, layer,
        };
      });
    }
    this.height = TOP + layers.size * LAYER_H + 24;
    this.svg.setAttribute("viewBox", `0 0 ${WIDTH} ${Math.max(HEIGHT, this.height)}`);
  }

  render(state) {
    this.svg.replaceChildren();
    if (!this.nodes.length) return;
    const now = state.t;
    const reached = this.stages.filter((stage) => stage.t <= now);
    const active = reached.length ? reached[reached.length - 1].layer : -1;
    const label = reached.length ? reached[reached.length - 1].label : "等待执行";

    this.svg.appendChild(el("text", {
      x: 30, y: 30, "font-size": 13, "font-weight": 700, fill: "var(--ink)",
    }, `当前阶段：${label}`));

    this.svg.appendChild(this._edges(active, now));
    this.svg.appendChild(this._nodes(active));
  }

  _edges(active, now) {
    const group = el("g");
    for (const edge of this.edges) {
      const from = this.position[edge.from];
      const to = this.position[edge.to];
      if (!from || !to) continue;
      const done = from.layer < active;
      const flowing = from.layer === active;
      const x0 = from.x + from.w / 2;
      const y0 = from.y + from.h;
      const x1 = to.x + to.w / 2;
      const y1 = to.y;
      const exchange = edge.kind === "exchange";
      group.appendChild(el("line", {
        x1: x0, y1: y0, x2: x1, y2: y1,
        stroke: exchange ? "var(--accent-2)" : "var(--line)",
        "stroke-width": exchange ? 1.6 : 1.2,
        opacity: done || flowing ? 0.85 : 0.28,
        "stroke-dasharray": exchange ? "5 4" : "none",
      }));
      if ((done || flowing) && edge.rows) {
        // Anchor the label near the source: many edges converging on one exchange
        // node would otherwise stack every label at the same midpoint.
        group.appendChild(el("text", {
          x: x0 + (x1 - x0) * 0.24 + 6, y: y0 + (y1 - y0) * 0.24 + 3, "font-size": 10,
          "font-family": "var(--mono)",
          fill: exchange ? "var(--accent-2)" : "var(--muted)",
        }, `${edge.rows} 行 · ${bytes(edge.bytes)}`));
      }
      if (flowing) {
        for (let dot = 0; dot < 3; dot += 1) {
          const progress = ((now * 0.012) + dot / 3) % 1;
          group.appendChild(el("circle", {
            cx: x0 + (x1 - x0) * progress, cy: y0 + (y1 - y0) * progress, r: 4,
            fill: exchange ? "var(--accent-2)" : "var(--accent)",
          }));
        }
      }
    }
    return group;
  }

  _nodes(active) {
    const group = el("g");
    for (const node of this.nodes) {
      const box = this.position[node.id];
      const style = KIND_STYLE[node.kind] || { color: "var(--muted)", icon: node.kind };
      const reached = box.layer <= active;
      const load = node.inputs ?? node.rows ?? 0;
      const slowest = this.peak[box.layer] > 0 && load === this.peak[box.layer]
        && this.layers.get(box.layer).length > 1;
      group.appendChild(el("rect", {
        x: box.x, y: box.y, width: box.w, height: box.h, rx: 11,
        fill: reached ? `color-mix(in srgb, ${style.color} 12%, transparent)` : "var(--surface)",
        stroke: slowest && reached ? "var(--accent-2)" : style.color,
        "stroke-width": slowest && reached ? 2.4 : 1.5,
        opacity: reached ? 1 : 0.4,
      }));
      group.appendChild(el("text", {
        x: box.x + 10, y: box.y + 19, "font-size": 11.5, "font-weight": 650,
        fill: style.color, opacity: reached ? 1 : 0.5,
      }, node.label));
      group.appendChild(el("text", {
        x: box.x + 10, y: box.y + 35, "font-size": 10.5, "font-family": "var(--mono)",
        fill: "var(--muted)", opacity: reached ? 1 : 0.5,
      }, `${node.rows ?? 0} 行`));
      if (slowest && reached) {
        group.appendChild(el("text", {
          x: box.x + box.w - 8, y: box.y + 35, "text-anchor": "end", "font-size": 9.5,
          fill: "var(--accent-2)",
        }, "最慢"));
      }
    }
    return group;
  }
}
