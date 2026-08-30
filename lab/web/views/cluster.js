/* The cluster view: nodes on a ring, messages as balls in flight, and a log
   matrix underneath. The matrix is where the commit rules become visible. */

const NS = "http://www.w3.org/2000/svg";
const CENTER = { x: 578, y: 232 };
const RADIUS = 163;
const NODE_R = 39;
const CLIENT = { x: 40, y: 186, w: 148, h: 78 };

const GROUP_COLORS = ["var(--accent)", "var(--accent-2)", "#b08900", "#4a6fa5"];
const TERM_COLORS = ["#0b6b6b", "#c14f2f", "#4a6fa5", "#7a5ea8", "#b08900", "#2f8f5b", "#a3486b"];

const KINDS = {
  RequestVote: { short: "RV", color: "#c14f2f", legend: "RequestVote 请求投票" },
  RequestVoteReply: { short: "RV↩", color: "#c14f2f", hollow: true },
  AppendEntries: { short: "AE", color: "#0b6b6b", legend: "AppendEntries 日志/心跳" },
  AppendEntriesReply: { short: "AE↩", color: "#0b6b6b", hollow: true },
  InstallSnapshot: { short: "SNAP", color: "#4a6fa5", legend: "InstallSnapshot 快照安装" },
  ReadIndexRequest: { short: "RI", color: "#2f8f5b", legend: "ReadIndex 读位置确认" },
  ReadIndexReply: { short: "RI↩", color: "#2f8f5b", hollow: true },
  Prepare: { short: "PRE", color: "#b08900", legend: "Prepare / Vote 表决" },
  Vote: { short: "VOTE", color: "#b08900", hollow: true },
  Decide: { short: "DEC", color: "#0b6b6b", legend: "Decide 决议下发" },
  StatusQuery: { short: "?", color: "var(--muted)", hollow: true, legend: "StatusQuery 询问决议" },
  BeginTxn: { short: "TXN", color: "#7a5ea8", legend: "客户端事务请求" },
  ClientRequest: { short: "REQ", color: "#7a5ea8", legend: "客户端请求与回复" },
  ClientRead: { short: "GET", color: "#7a5ea8", legend: "客户端读请求" },
  ClientReply: { short: "RSP", color: "#7a5ea8", hollow: true },
};

// A node is drawn from its reported state, so consensus roles and transaction
// states share one renderer.
const STATE_STYLE = {
  leader: { fill: "var(--accent)", text: "#fff", stroke: "var(--accent)", caption: "Leader" },
  candidate: { stroke: "var(--accent-2)", dash: "7 4", width: 3, caption: "Candidate" },
  follower: { stroke: "var(--line)", caption: "Follower" },
  coordinator: { fill: "#7a5ea8", text: "#fff", stroke: "#7a5ea8", caption: "Coordinator 协调者" },
  backup: { stroke: "#7a5ea8", dash: "6 4", caption: "Backup 备用协调者" },
  idle: { stroke: "var(--line)", caption: "idle 空闲" },
  prepared: {
    fill: "color-mix(in srgb, #b08900 22%, transparent)", stroke: "#b08900", width: 3,
    caption: "prepared 持锁等待",
  },
  committed: {
    fill: "color-mix(in srgb, var(--accent) 18%, transparent)", stroke: "var(--accent)",
    caption: "committed 已提交",
  },
  aborted: { stroke: "var(--accent-2)", dash: "4 4", caption: "aborted 已回滚" },
  participant: { stroke: "#4a6fa5", caption: "参与者 participant" },
  clock: { stroke: "#b08900", dash: "6 4", caption: "时间 / 版本服务" },
  record: { stroke: "#2f8f5b", dash: "6 4", caption: "记录 / 日志服务" },
  shard: { stroke: "#4a6fa5", caption: "分片 shard" },
};

export function termColor(term) {
  return TERM_COLORS[(Math.max(0, term) - 1 + TERM_COLORS.length) % TERM_COLORS.length];
}

function el(name, attrs = {}, text = "") {
  const node = document.createElementNS(NS, name);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text !== "") node.textContent = text;
  return node;
}

export class ClusterView {
  constructor(svg, matrix) {
    this.svg = svg;
    this.matrix = matrix;
    this.positions = {};
  }

  setTrace(trace) {
    this.trace = trace;
    const ids = trace.nodes.map((node) => node.id);
    this.positions = {};
    // Without a client box the ring can use the space on the left.
    const center = { x: trace.clients.length ? CENTER.x : CENTER.x - 96, y: CENTER.y };
    ids.forEach((id, index) => {
      const angle = (-Math.PI / 2) + (index * 2 * Math.PI) / ids.length;
      this.positions[id] = {
        x: center.x + RADIUS * Math.cos(angle),
        y: center.y + RADIUS * Math.sin(angle),
      };
    });
    const kinds = new Set(trace.events.filter((e) => e.type === "msg.send").map((e) => e.kind));
    this.legend = [...kinds]
      .map((kind) => KINDS[kind])
      .filter((style) => style && style.legend)
      .slice(0, 4);
    this.clientId = trace.clients[0]?.id || null;
    if (this.clientId) {
      this.positions[this.clientId] = { x: CLIENT.x + CLIENT.w / 2, y: CLIENT.y + CLIENT.h / 2 };
    }
  }

  render(state) {
    this.svg.replaceChildren();
    const groupOf = (id) => {
      if (!state.partitions) return -1;
      return state.partitions.findIndex((group) => group.includes(id));
    };

    this.svg.appendChild(this._edges(state, groupOf));
    if (this.clientId) this.svg.appendChild(this._client(state));
    this.svg.appendChild(this._nodes(state, groupOf));
    this.svg.appendChild(this._flights(state));
    this.svg.appendChild(this._legend());
    this._matrix(state);
  }

  /* -- pieces -------------------------------------------------------------- */

  _edges(state, groupOf) {
    const group = el("g", { opacity: "0.9" });
    const ids = Object.keys(this.positions).filter((id) => id !== this.clientId);
    for (let i = 0; i < ids.length; i += 1) {
      for (let j = i + 1; j < ids.length; j += 1) {
        const a = this.positions[ids[i]];
        const b = this.positions[ids[j]];
        const cut = state.partitions && groupOf(ids[i]) !== groupOf(ids[j]);
        group.appendChild(el("line", {
          x1: a.x, y1: a.y, x2: b.x, y2: b.y,
          stroke: cut ? "var(--accent-2)" : "var(--line)",
          "stroke-width": cut ? 1.4 : 1,
          "stroke-dasharray": cut ? "3 6" : "none",
          opacity: cut ? 0.55 : 0.5,
        }));
        if (cut) {
          group.appendChild(el("text", {
            x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 + 4, "text-anchor": "middle",
            fill: "var(--accent-2)", "font-size": 12, opacity: 0.75,
          }, "✕"));
        }
      }
    }
    return group;
  }

  _nodes(state, groupOf) {
    const group = el("g");
    for (const [id, position] of Object.entries(this.positions)) {
      if (id === this.clientId) continue;
      const node = state.nodes[id];
      const style = STATE_STYLE[node.state] || STATE_STYLE.follower;
      const solid = Boolean(style.fill && style.text);
      const ring = state.partitions ? GROUP_COLORS[groupOf(id) % GROUP_COLORS.length] : null;
      const item = el("g", { opacity: node.alive ? 1 : 0.42 });

      if (ring) {
        item.appendChild(el("circle", {
          cx: position.x, cy: position.y, r: NODE_R + 8, fill: "none",
          stroke: ring, "stroke-width": 2, "stroke-dasharray": "4 5", opacity: 0.8,
        }));
      }
      item.appendChild(el("circle", {
        cx: position.x, cy: position.y, r: NODE_R,
        fill: node.alive && style.fill ? style.fill : "var(--surface)",
        stroke: node.alive ? (style.stroke || "var(--line)") : "var(--muted)",
        "stroke-width": node.alive ? (style.width || 2) : 2,
        "stroke-dasharray": node.alive ? (style.dash || "none") : "5 4",
      }));
      item.appendChild(el("text", {
        x: position.x, y: position.y + (node.term > 0 ? 2 : 7), "text-anchor": "middle",
        "font-size": 19, "font-weight": 700,
        fill: node.alive && solid ? style.text : "var(--ink)",
      }, id));
      if (node.term > 0) {
        item.appendChild(el("text", {
          x: position.x, y: position.y + 20, "text-anchor": "middle", "font-size": 11,
          "font-family": "var(--mono)",
          fill: node.alive && solid ? "#d7eeee" : "var(--muted)",
        }, `term ${node.term}`));
      }

      const caption = node.alive ? style.caption : "崩溃 crashed";
      item.appendChild(el("text", {
        x: position.x, y: position.y + NODE_R + 17, "text-anchor": "middle", "font-size": 11,
        fill: node.alive ? (style.stroke || "var(--muted)") : "var(--muted)",
        "font-weight": node.alive && style.fill ? 700 : 500,
      }, caption));
      const detail = node.detail || node.machine;
      if (detail) {
        item.appendChild(el("text", {
          x: position.x, y: position.y + NODE_R + 31, "text-anchor": "middle",
          "font-size": 10, "font-family": "var(--mono)", fill: "var(--muted)",
        }, detail));
      }
      if (node.base) {
        item.appendChild(el("text", {
          x: position.x, y: position.y + NODE_R + 44, "text-anchor": "middle",
          "font-size": 10, "font-family": "var(--mono)", fill: "#4a6fa5",
        }, `快照 ≤ ${node.base}`));
      }
      if (!node.alive) {
        item.appendChild(el("text", {
          x: position.x, y: position.y - NODE_R - 8, "text-anchor": "middle",
          "font-size": 15, fill: "var(--accent-2)",
        }, "✕"));
      }
      group.appendChild(item);
    }
    return group;
  }

  _client(state) {
    const group = el("g");
    const pending = Object.values(state.client).filter((op) => op.status === "pending");
    const finished = Object.values(state.client).filter((op) => op.status !== "pending");
    const latest = pending[pending.length - 1] || finished[finished.length - 1];
    group.appendChild(el("rect", {
      x: CLIENT.x, y: CLIENT.y, width: CLIENT.w, height: CLIENT.h, rx: 14,
      fill: "var(--surface)", stroke: "var(--line)", "stroke-width": 2,
    }));
    group.appendChild(el("text", {
      x: CLIENT.x + 14, y: CLIENT.y + 24, "font-size": 13, "font-weight": 700, fill: "var(--ink)",
    }, "客户端 client"));
    if (latest) {
      const colors = { ok: "var(--ok)", info: "var(--muted)", fail: "var(--accent-2)", pending: "var(--accent)" };
      group.appendChild(el("text", {
        x: CLIENT.x + 14, y: CLIENT.y + 44, "font-size": 11, "font-family": "var(--mono)",
        fill: "var(--muted)",
      }, `${latest.opId} ${latest.op}`));
      const label = { ok: "ok", info: "unknown", fail: "fail", pending: "等待中…" }[latest.status];
      group.appendChild(el("circle", {
        cx: CLIENT.x + 19, cy: CLIENT.y + 60, r: 5, fill: colors[latest.status],
      }));
      group.appendChild(el("text", {
        x: CLIENT.x + 31, y: CLIENT.y + 64, "font-size": 12, "font-weight": 600,
        fill: colors[latest.status],
      }, label));
    }
    const counts = { ok: 0, info: 0, fail: 0 };
    for (const op of finished) counts[op.status] = (counts[op.status] || 0) + 1;
    group.appendChild(el("text", {
      x: CLIENT.x, y: CLIENT.y + CLIENT.h + 20, "font-size": 11, "font-family": "var(--mono)",
      fill: "var(--muted)",
    }, `ok ${counts.ok} · unknown ${counts.info} · fail ${counts.fail}`));
    return group;
  }

  _flights(state) {
    const group = el("g");
    for (const flight of state.flights) {
      const from = this.positions[flight.from];
      const to = this.positions[flight.to];
      if (!from || !to) continue;
      const style = KINDS[flight.kind] || { short: flight.kind.slice(0, 3), color: "var(--muted)" };
      const x = from.x + (to.x - from.x) * flight.progress;
      const y = from.y + (to.y - from.y) * flight.progress;
      const fading = flight.ended === "drop" ? Math.max(0.25, 1 - flight.progress * 0.5) : 1;
      group.appendChild(el("circle", {
        cx: x, cy: y, r: 7, fill: style.hollow ? "var(--surface)" : style.color,
        stroke: style.color, "stroke-width": 2, opacity: fading,
      }));
      group.appendChild(el("text", {
        x: x + 11, y: y - 8, "font-size": 10, "font-family": "var(--mono)",
        fill: style.color, opacity: fading,
      }, style.short + entryHint(flight.label)));
    }
    for (const burst of state.bursts) {
      const from = this.positions[burst.from];
      const to = this.positions[burst.to];
      if (!from || !to) continue;
      const x = from.x + (to.x - from.x) * 0.5;
      const y = from.y + (to.y - from.y) * 0.5;
      group.appendChild(el("text", {
        x, y, "text-anchor": "middle", "font-size": 16, fill: "var(--accent-2)",
      }, "✕"));
      group.appendChild(el("text", {
        x, y: y + 13, "text-anchor": "middle", "font-size": 10, fill: "var(--accent-2)",
        "font-family": "var(--mono)",
      }, dropCause(burst.cause)));
    }
    return group;
  }

  _legend() {
    const items = this.legend || [];
    const group = el("g", { transform: `translate(22, ${412 - items.length * 20})` });
    items.forEach((style, index) => {
      const y = index * 20;
      group.appendChild(el("circle", { cx: 6, cy: y, r: 6, fill: style.color }));
      group.appendChild(el("text", {
        x: 20, y: y + 4, "font-size": 11, fill: "var(--muted)",
      }, `${style.short} · ${style.legend}`));
    });
    return group;
  }

  /* -- log matrix ---------------------------------------------------------- */

  _matrix(state) {
    const ids = Object.keys(state.nodes);
    const width = Math.max(0, ...ids.map((id) => state.nodes[id].log.length),
                           ...Object.keys(state.committed).map(Number));
    if (width === 0) {
      // Nothing replicated yet: an empty grid is noise, not information.
      this.matrix.replaceChildren();
      return;
    }
    const rows = [];

    const axis = document.createElement("div");
    axis.className = "log-axis";
    const spacer = document.createElement("span");
    spacer.className = "who";
    axis.appendChild(spacer);
    for (let index = 1; index <= width; index += 1) {
      const cell = document.createElement("span");
      cell.textContent = `索引 ${index}`;
      axis.appendChild(cell);
    }
    rows.push(axis);

    for (const id of ids) {
      const node = state.nodes[id];
      const row = document.createElement("div");
      row.className = "log-row"
        + (node.alive && node.state === "leader" ? " is-leader" : "")
        + (node.alive ? "" : " is-dead");
      const who = document.createElement("div");
      who.className = "who";
      who.textContent = id;
      row.appendChild(who);

      const cells = document.createElement("div");
      cells.className = "log-cells";
      for (let index = 1; index <= width; index += 1) {
        const entry = node.log[index - 1];
        const cell = document.createElement("div");
        const committed = state.committed[index];
        if (index <= (node.base || 0)) {
          // Draw the compacted prefix as one band, labelled only at its edge.
          cell.className = "cell snapshot";
          cell.innerHTML = index === node.base ? `<b>快照 ≤ ${node.base}</b>snapshot` : "·";
          cell.title = `索引 ${index} 已压缩进快照（lastIncludedIndex = ${node.base}）`;
        } else if (!entry) {
          cell.className = "cell empty";
        } else {
          const color = termColor(entry.term);
          const conflict = committed && committed.term !== entry.term;
          cell.className = "cell" + (conflict ? " conflict" : committed ? " committed" : "");
          if (!conflict) {
            // Term colours come from the palette; conflicts are striped in CSS instead,
            // so a term that happens to be rust cannot be mistaken for an overwrite.
            cell.style.background = `color-mix(in srgb, ${color} 14%, transparent)`;
            cell.style.borderColor = `color-mix(in srgb, ${color} 45%, transparent)`;
            cell.style.color = color;
          }
          cell.innerHTML = `<b>${entry.cmd}</b>term ${entry.term}`;
          cell.title = conflict
            ? `索引 ${index} 曾以 term ${committed.term} / ${committed.cmd} 被提交，现在是 term ${entry.term} / ${entry.cmd}`
            : `索引 ${index}，term ${entry.term}，${entry.cmd}`;
        }
        cells.appendChild(cell);
      }
      row.appendChild(cells);
      rows.push(row);
    }
    this.matrix.replaceChildren(...rows);
  }
}

function entryHint(label) {
  const match = /\+(\d+)/.exec(label || "");
  return match && match[1] !== "0" ? ` +${match[1]}` : "";
}

function dropCause(cause) {
  return { partition: "分区丢弃", loss: "丢包", reply_lost: "回复丢失" }[cause] || "丢弃";
}
