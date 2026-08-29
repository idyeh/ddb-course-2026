"""The trace is the only contract between the Python simulator and the web UI.

The browser never simulates anything: it replays the event list produced here.
That is what makes the classroom animation, the command line run, the unit test
and the lab report look at exactly the same evidence.
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = 1

# Levels used by `note` events. The UI renders them differently and can pause on
# each of them, which is how a lecturer walks a class through a run.
NOTE_LEVELS = ("phase", "teach", "warn", "violation")


class Trace:
    def __init__(
        self,
        scenario: str,
        seed: int,
        params: dict[str, Any],
        views: list[str],
        title: str = "",
    ) -> None:
        self.scenario = scenario
        self.seed = seed
        self.params = dict(params)
        self.views = list(views)
        self.title = title
        self.nodes: list[dict[str, Any]] = []
        self.clients: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.facts: dict[str, Any] = {}
        self._sequence = 0
        self._last_tick = 0

    # -- description of the stage ------------------------------------------------

    def add_node(self, node_id: str, kind: str = "replica", **fields: Any) -> None:
        self.nodes.append({"id": node_id, "kind": kind, **fields})

    def add_client(self, client_id: str, **fields: Any) -> None:
        self.clients.append({"id": client_id, **fields})

    # -- events -----------------------------------------------------------------

    def record(self, at: int, kind: str, /, **fields: Any) -> dict[str, Any]:
        # Positional-only so an event may carry fields named `at` or `kind`.
        if at < self._last_tick:
            raise ValueError(f"event at {at} recorded after {self._last_tick}")
        self._last_tick = at
        self._sequence += 1
        event = {"t": at, "seq": self._sequence, "type": kind, **fields}
        self.events.append(event)
        return event

    def note(
        self,
        at: int,
        text: str,
        level: str = "teach",
        ref: str = "",
        title: str = "",
    ) -> dict[str, Any]:
        if level not in NOTE_LEVELS:
            raise ValueError(f"unknown note level {level!r}")
        return self.record(at, "note", level=level, text=text, ref=ref, title=title)

    # -- output -----------------------------------------------------------------

    @property
    def duration(self) -> int:
        return self._last_tick

    def to_dict(self, summary: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "scenario": self.scenario,
            "title": self.title,
            "seed": self.seed,
            "params": self.params,
            "views": self.views,
            "duration": self.duration,
            "nodes": self.nodes,
            "clients": self.clients,
            "events": self.events,
            "facts": self.facts,
            "summary": summary or {},
        }
