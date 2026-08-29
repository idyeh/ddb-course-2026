/* The player replays a trace. It contains no protocol logic at all: every fact on
   screen was decided by the Python simulator and recorded as an event. */

const NOTE_PAUSE_LEVELS = new Set(["phase", "warn", "violation"]);

export class Player {
  constructor(options) {
    this.onRender = options.onRender;
    this.onNotes = options.onNotes;
    this.onEvents = options.onEvents;
    this.trace = null;
    this.t = 0;
    this.playing = false;
    this.speed = 140;
    this.pauseOnNote = true;
    this._frame = null;
    this._last = 0;
  }

  load(trace) {
    this.trace = trace;
    this.duration = trace.duration || 1;
    this.flights = buildFlights(trace);
    this._base = emptyState(trace);
    this._state = cloneState(this._base);
    this.pause();
    this.seek(0);
  }

  /* -- transport ----------------------------------------------------------- */

  play() {
    if (!this.trace || this.playing) return;
    if (this.t >= this.duration) this.seek(0);
    this.playing = true;
    this._last = performance.now();
    const step = (now) => {
      if (!this.playing) return;
      const dt = Math.min(0.12, (now - this._last) / 1000);
      this._last = now;
      const next = this.t + dt * this.speed;
      const note = this.pauseOnNote ? this._noteBetween(this.t, next) : null;
      if (note) {
        this.seek(note.t);
        this.pause();
        return;
      }
      if (next >= this.duration) {
        this.seek(this.duration);
        this.pause();
        return;
      }
      this.seek(next);
      this._frame = requestAnimationFrame(step);
    };
    this._frame = requestAnimationFrame(step);
  }

  pause() {
    this.playing = false;
    if (this._frame) cancelAnimationFrame(this._frame);
    this._frame = null;
    this.onRender?.(this.snapshot(), this);
  }

  toggle() { this.playing ? this.pause() : this.play(); }

  seek(t) {
    if (!this.trace) return;
    this.t = Math.max(0, Math.min(this.duration, t));
    this._advance(this.t);
    this.onRender?.(this.snapshot(), this);
  }

  /** Jump to the previous or next recorded event, so a class can walk a run. */
  stepEvent(direction) {
    if (!this.trace) return;
    const events = this.trace.events;
    if (direction > 0) {
      const found = events.find((event) => event.t > this.t + 0.001);
      this.seek(found ? found.t : this.duration);
    } else {
      let target = 0;
      for (const event of events) {
        if (event.t < this.t - 0.001) target = event.t; else break;
      }
      this.seek(target);
    }
    this.pause();
  }

  _noteBetween(from, to) {
    return this.trace.events.find(
      (event) => event.type === "note" && event.t > from && event.t <= to &&
                 NOTE_PAUSE_LEVELS.has(event.level));
  }

  /* -- state reconstruction ------------------------------------------------ */

  _advance(t) {
    if (t < this._state.t) this._state = cloneState(this._base);
    const events = this.trace.events;
    while (this._state.cursor < events.length && events[this._state.cursor].t <= t) {
      applyEvent(this._state, events[this._state.cursor]);
      this._state.cursor += 1;
    }
    this._state.t = t;
  }

  snapshot() {
    const t = this.t;
    const flights = [];
    const bursts = [];
    for (const flight of this.flights) {
      if (flight.t0 <= t && t < flight.t1) {
        const span = Math.max(1, flight.t1 - flight.t0);
        flights.push({ ...flight, progress: (t - flight.t0) / span });
      } else if (flight.ended === "drop" && t >= flight.t1 && t < flight.t1 + 26) {
        bursts.push(flight);
      }
    }
    return { ...this._state, t, flights, bursts, duration: this.duration };
  }
}

/* -- helpers --------------------------------------------------------------- */

function emptyState(trace) {
  const nodes = {};
  for (const node of trace.nodes) {
    nodes[node.id] = {
      id: node.id, state: "follower", term: 0, alive: true,
      commit: 0, votedFor: null, log: [], machine: "",
    };
  }
  return { t: 0, cursor: 0, nodes, partitions: null, committed: {}, notes: [], client: {} };
}

function cloneState(state) {
  const nodes = {};
  for (const [id, node] of Object.entries(state.nodes)) nodes[id] = { ...node, log: [...node.log] };
  return {
    t: state.t, cursor: state.cursor, nodes,
    partitions: state.partitions ? state.partitions.map((group) => [...group]) : null,
    committed: { ...state.committed }, notes: [...state.notes],
    client: Object.fromEntries(Object.entries(state.client).map(([k, v]) => [k, { ...v }])),
  };
}

function applyEvent(state, event) {
  const node = event.node ? state.nodes[event.node] : null;
  switch (event.type) {
    case "node.state":
      if (node) Object.assign(node, {
        state: event.state, term: event.term, votedFor: event.votedFor,
        commit: event.commit, alive: event.alive,
      });
      break;
    case "node.crash":
      if (node) Object.assign(node, { alive: false, state: "follower", commit: 0, machine: "" });
      break;
    case "node.restart":
      if (node) node.alive = true;
      break;
    case "log.append":
      if (node) {
        node.log.length = event.index - 1;
        node.log.push({ index: event.index, term: event.term, cmd: event.cmd, opId: event.opId });
      }
      break;
    case "log.truncate":
      if (node) node.log.length = Math.max(0, event.from - 1);
      break;
    case "log.commit":
      if (!state.committed[event.index]) {
        state.committed[event.index] = { term: event.term, cmd: event.cmd, by: event.node, t: event.t };
      }
      break;
    case "state.apply":
      if (node) node.machine = event.state;
      break;
    case "fault":
      state.partitions = event.kind === "partition" ? event.groups.map((g) => [...g]) : null;
      break;
    case "note":
      state.notes.push(event);
      break;
    case "client.call":
      state.client[event.reqId] = {
        reqId: event.reqId, opId: event.opId, op: event.op, target: event.target,
        calledAt: event.t, status: "pending",
      };
      break;
    case "client.return": {
      const entry = state.client[event.reqId];
      if (entry) Object.assign(entry, { status: event.status, returnedAt: event.t });
      break;
    }
    default:
      break;
  }
}

function buildFlights(trace) {
  const open = new Map();
  const flights = [];
  for (const event of trace.events) {
    if (event.type === "msg.send") {
      const flight = {
        id: event.id, from: event.from, to: event.to, kind: event.kind,
        label: event.label, t0: event.t, t1: event.eta, ended: "deliver", cause: "",
      };
      open.set(event.id, flight);
      flights.push(flight);
    } else if (event.type === "msg.deliver" || event.type === "msg.drop") {
      const flight = open.get(event.id);
      if (flight) {
        flight.t1 = Math.max(event.t, flight.t0 + 1);
        flight.ended = event.type === "msg.drop" ? "drop" : "deliver";
        flight.cause = event.cause || "";
        open.delete(event.id);
      }
    }
  }
  return flights;
}
