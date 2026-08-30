"""Scenario registry: one entry per classroom demonstration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .params import ParamSet
from .trace import Trace

RunFn = Callable[[dict[str, Any], int], "RunResult"]


PASS = "pass"
FAIL = "fail"
INCONCLUSIVE = "inconclusive"


@dataclass
class CheckResult:
    """A check has three outcomes, not two.

    `inconclusive` exists because a bounded checker that ran out of budget has not
    established anything. Encoding that as a pass would let a green panel stand in
    for a proof obligation nobody discharged.
    """

    name: str
    ok: bool
    detail: str = ""
    counterexample: list[dict[str, Any]] = field(default_factory=list)
    status: str = ""

    def __post_init__(self) -> None:
        if not self.status:
            self.status = PASS if self.ok else FAIL
        if self.status == INCONCLUSIVE:
            self.ok = True  # not a violation, but not a pass either

    @classmethod
    def inconclusive(cls, name: str, detail: str) -> "CheckResult":
        return cls(name, True, detail, status=INCONCLUSIVE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "status": self.status,
            "detail": self.detail,
            "counterexample": self.counterexample,
        }


@dataclass
class RunResult:
    trace: Trace
    checks: list[CheckResult] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.trace.to_dict(
            {
                "metrics": self.metrics,
                "checks": [check.to_dict() for check in self.checks],
                "violations": [check.name for check in self.checks
                               if check.status == FAIL],
                "inconclusive": [check.name for check in self.checks
                                 if check.status == INCONCLUSIVE],
            }
        )


@dataclass(frozen=True)
class Prediction:
    """Shown before the run so the class can commit to an answer first."""

    question: str
    options: tuple[str, ...]
    answer: int
    why: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "options": list(self.options),
            "answer": self.answer,
            "why": self.why,
        }


@dataclass(frozen=True)
class Scenario:
    id: str
    lecture: str
    order: int
    title: str
    subtitle: str
    views: tuple[str, ...]
    params: ParamSet
    run: RunFn
    question: str = ""
    takeaway: str = ""
    prediction: Prediction | None = None
    invariants: tuple[str, ...] = ()
    faults: tuple[str, ...] = ()
    caveat: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "lecture": self.lecture,
            "order": self.order,
            "title": self.title,
            "subtitle": self.subtitle,
            "views": list(self.views),
            "params": self.params.to_list(),
            "rules": [rule.to_dict() for rule in self.params.rules],
            "question": self.question,
            "takeaway": self.takeaway,
            "prediction": self.prediction.to_dict() if self.prediction else None,
            "invariants": list(self.invariants),
            "faults": list(self.faults),
            "caveat": self.caveat,
        }


_REGISTRY: dict[str, Scenario] = {}


def register(scenario: Scenario) -> Scenario:
    if scenario.id in _REGISTRY:
        raise ValueError(f"scenario {scenario.id!r} is already registered")
    _REGISTRY[scenario.id] = scenario
    return scenario


def get(scenario_id: str) -> Scenario:
    load_all()
    if scenario_id not in _REGISTRY:
        raise KeyError(f"unknown scenario {scenario_id!r}")
    return _REGISTRY[scenario_id]


def all_scenarios() -> list[Scenario]:
    load_all()
    return sorted(_REGISTRY.values(), key=lambda item: (item.order, item.id))


_loaded = False


def load_all() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from . import scenarios  # noqa: F401  (importing registers every scenario)

    scenarios.load()
