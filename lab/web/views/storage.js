/* WAL strip, disk pages, buffer pages, and the recovery tables.

   The point the picture has to make is that disk and buffer can disagree, and
   that recovery can only use what is durable in the log. */

const NS = "http://www.w3.org/2000/svg";
const WIDTH = 960;
const HEIGHT = 470;
const WAL_X = 128;
const WAL_Y = 74;
const CELL_W = 62;
const CELL_H = 44;

const KIND_STYLE = {
  update: { color: "#0b6b6b", label: "更新" },
  commit: { color: "#17795e", label: "提交" },
  checkpoint: { color: "#4a6fa5", label: "检查点" },
  clr: { color: "#7a5ea8", label: "CLR" },
};

const PHASE_LABEL = {
  analysis: "分析 Analysis", redo: "重做 Redo", undo: "撤销 Undo", done: "恢复完成",
};

function el(name, attrs = {}, text = "") {
  const node = document.createElementNS(NS, name);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text !== "") node.textContent = text;
  return node;
}

export class StorageView {
  constructor(svg) {
    this.svg = svg;
  }

  setTrace(trace) {
    this.trace = trace;
    this.svg.setAttribute("viewBox", `0 0 ${WIDTH} ${HEIGHT}`);
  }

  _replay(now) {
    const state = {
      records: [], durable: 0, disk: {}, buffer: {}, phase: "", scan: 0,
      roles: {}, dirty: {}, touched: {}, crashed: false, step: "",
    };
    for (const event of this.trace.events) {
      if (event.t > now) break;
      switch (event.type) {
        case "workload.step": state.step = `${event.step}. ${event.label}`; break;
        case "wal.append":
          state.records[event.lsn - 1] = event;
          state.records.length = event.lsn;
          break;
        case "wal.flush": state.durable = event.upto; break;
        case "page.state":
          state.disk[event.page] = { values: { ...event.values }, pageLSN: event.pageLSN };
          state.buffer[event.page] = { values: { ...event.values }, pageLSN: event.pageLSN };
          break;
        case "page.update":
          state.buffer[event.page] = state.buffer[event.page] || { values: {}, pageLSN: 0 };
          state.buffer[event.page].values[event.key] = event.value;
          state.buffer[event.page].pageLSN = event.pageLSN;
          break;
        case "page.flush":
          state.disk[event.page] = { values: { ...event.values }, pageLSN: event.pageLSN };
          break;
        case "fault":
          if (event.kind === "crash" || event.kind === "crash_during_recovery") {
            state.crashed = true;
            state.records.length = Math.min(state.records.length, event.durableLsn);
            state.buffer = JSON.parse(JSON.stringify(state.disk));
            state.phase = "";
            state.roles = {}; state.dirty = {}; state.touched = {};
          }
          break;
        case "recovery.phase": state.phase = event.phase; break;
        case "recovery.scan": state.scan = event.lsn; break;
        case "recovery.txn": state.roles[event.txn] = event.role; break;
        case "recovery.dirty": state.dirty[event.page] = event.recLSN; break;
        case "recovery.redo":
          state.touched[event.lsn] = event.applied ? "redo" : "skip";
          state.scan = event.lsn;
          break;
        case "recovery.undo":
          state.touched[event.lsn] = "undo";
          state.scan = event.lsn;
          break;
        default: break;
      }
      // Redo and undo events name an LSN; the page comes from that log record.
      const target = (event.type === "recovery.redo" && event.applied) ||
                     event.type === "recovery.undo"
        ? state.records[event.lsn - 1] : null;
      if (target && state.buffer[target.page]) {
        state.buffer[target.page].values[target.key] = event.value;
        if (event.type === "recovery.redo") state.buffer[target.page].pageLSN = event.lsn;
      }
    }
    return state;
  }

  render(snapshot) {
    this.svg.replaceChildren();
    const state = this._replay(snapshot.t);
    this.svg.appendChild(this._header(state));
    this.svg.appendChild(this._wal(state));
    this.svg.appendChild(this._pages(state));
    this.svg.appendChild(this._tables(state));
  }

  _header(state) {
    const group = el("g");
    group.appendChild(el("text", {
      x: 30, y: 30, "font-size": 13, "font-weight": 700, fill: "var(--ink)",
    }, state.phase ? PHASE_LABEL[state.phase] || state.phase : (state.step || "正常运行")));
    group.appendChild(el("text", {
      x: 30, y: 48, "font-size": 11, "font-family": "var(--mono)", fill: "var(--muted)",
    }, `日志已持久化到 LSN ${state.durable}`));
    if (state.crashed) {
      group.appendChild(el("text", {
        x: WIDTH - 30, y: 30, "text-anchor": "end", "font-size": 12, "font-weight": 700,
        fill: "var(--accent-2)",
      }, "已崩溃：未落盘的日志尾部已消失"));
    }
    return group;
  }

  _wal(state) {
    const group = el("g");
    group.appendChild(el("text", {
      x: 30, y: WAL_Y + 26, "font-size": 12, "font-weight": 650, fill: "var(--ink)",
    }, "预写日志 WAL"));
    state.records.forEach((record, index) => {
      if (!record) return;
      const x = WAL_X + index * CELL_W;
      const style = KIND_STYLE[record.kind] || { color: "var(--muted)", label: record.kind };
      const durable = record.lsn <= state.durable;
      const mark = state.touched[record.lsn];
      group.appendChild(el("rect", {
        x, y: WAL_Y, width: CELL_W - 6, height: CELL_H, rx: 8,
        fill: durable ? `color-mix(in srgb, ${style.color} 14%, transparent)` : "transparent",
        stroke: mark === "undo" ? "var(--accent-2)"
              : mark === "redo" ? "var(--accent)" : style.color,
        "stroke-width": mark ? 2.6 : 1.4,
        "stroke-dasharray": durable ? "none" : "4 3",
        opacity: durable ? 1 : 0.55,
      }));
      group.appendChild(el("text", {
        x: x + (CELL_W - 6) / 2, y: WAL_Y + 17, "text-anchor": "middle", "font-size": 10,
        "font-family": "var(--mono)", fill: style.color,
      }, `LSN ${record.lsn}`));
      group.appendChild(el("text", {
        x: x + (CELL_W - 6) / 2, y: WAL_Y + 33, "text-anchor": "middle", "font-size": 9.5,
        fill: "var(--ink)",
      }, record.label));
      if (mark) {
        group.appendChild(el("text", {
          x: x + (CELL_W - 6) / 2, y: WAL_Y - 6, "text-anchor": "middle", "font-size": 9.5,
          fill: mark === "undo" ? "var(--accent-2)" : "var(--accent)",
        }, { redo: "重做", skip: "跳过", undo: "撤销" }[mark]));
      }
    });
    if (state.scan) {
      const x = WAL_X + (state.scan - 1) * CELL_W + (CELL_W - 6) / 2;
      group.appendChild(el("path", {
        d: `M ${x - 6} ${WAL_Y + CELL_H + 12} L ${x + 6} ${WAL_Y + CELL_H + 12} L ${x} ${WAL_Y + CELL_H + 3} z`,
        fill: "var(--accent)",
      }));
    }
    return group;
  }

  _pages(state) {
    const group = el("g");
    const rows = [
      ["磁盘 disk（崩溃后仍在的内容）", state.disk, 168],
      ["缓冲池 buffer（进程内存）", state.buffer, 272],
    ];
    for (const [title, store, y] of rows) {
      group.appendChild(el("text", {
        x: 30, y: y + 26, "font-size": 12, "font-weight": 650, fill: "var(--ink)",
      }, title.split("（")[0]));
      group.appendChild(el("text", {
        x: 30, y: y + 42, "font-size": 10, fill: "var(--muted)",
      }, "（" + (title.split("（")[1] || "")));
      const pages = Object.keys(store).sort();
      pages.forEach((page, index) => {
        const x = WAL_X + index * 230;
        const values = store[page].values || {};
        group.appendChild(el("rect", {
          x, y, width: 210, height: 66, rx: 11,
          fill: "var(--surface)", stroke: "var(--line)", "stroke-width": 1.6,
        }));
        group.appendChild(el("text", {
          x: x + 14, y: y + 24, "font-size": 13, "font-weight": 700, fill: "var(--ink)",
        }, page));
        group.appendChild(el("text", {
          x: x + 14, y: y + 44, "font-size": 12, "font-family": "var(--mono)",
          fill: "var(--accent)",
        }, Object.entries(values).map(([key, value]) => `${key}=${value}`).join("  ")));
        group.appendChild(el("text", {
          x: x + 196, y: y + 24, "text-anchor": "end", "font-size": 10,
          "font-family": "var(--mono)", fill: "var(--muted)",
        }, `pageLSN ${store[page].pageLSN}`));
        const recLSN = state.dirty[page];
        if (recLSN !== undefined && y > 200) {
          group.appendChild(el("text", {
            x: x + 196, y: y + 44, "text-anchor": "end", "font-size": 10,
            "font-family": "var(--mono)", fill: "#4a6fa5",
          }, `recLSN ${recLSN}`));
        }
      });
    }
    return group;
  }

  _tables(state) {
    const group = el("g");
    const entries = Object.entries(state.roles).sort();
    group.appendChild(el("text", {
      x: 30, y: 396, "font-size": 12, "font-weight": 650, fill: "var(--ink)",
    }, "事务表"));
    if (!entries.length) {
      group.appendChild(el("text", {
        x: WAL_X, y: 396, "font-size": 11, fill: "var(--muted)",
      }, "（分析阶段开始后重建）"));
      return group;
    }
    entries.forEach(([txn, role], index) => {
      const x = WAL_X + index * 150;
      const winner = role === "winner";
      group.appendChild(el("rect", {
        x, y: 378, width: 132, height: 30, rx: 9,
        fill: winner ? "color-mix(in srgb, var(--accent) 14%, transparent)"
                     : "color-mix(in srgb, var(--accent-2) 14%, transparent)",
        stroke: winner ? "var(--accent)" : "var(--accent-2)", "stroke-width": 1.4,
      }));
      group.appendChild(el("text", {
        x: x + 66, y: 398, "text-anchor": "middle", "font-size": 12, "font-weight": 650,
        fill: winner ? "var(--accent)" : "var(--accent-2)",
      }, `${txn} · ${winner ? "winner 重做" : "loser 撤销"}`));
    });
    return group;
  }
}
