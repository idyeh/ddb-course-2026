"""Parameter schemas. The instructor's configuration form is generated from these."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Param:
    name: str
    label: str
    kind: str  # int | float | bool | choice
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    choices: tuple[tuple[Any, str], ...] = ()
    help: str = ""
    axis: bool = False  # may be used as the axis of a side-by-side comparison

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "default": self.default,
            "min": self.minimum,
            "max": self.maximum,
            "step": self.step,
            "choices": [{"value": value, "label": label} for value, label in self.choices],
            "help": self.help,
            "axis": self.axis,
        }

    def coerce(self, raw: Any) -> Any:
        if self.kind == "int":
            value = int(raw)
        elif self.kind == "float":
            value = float(raw)
        elif self.kind == "bool":
            value = raw if isinstance(raw, bool) else str(raw).lower() in {"1", "true", "yes", "on"}
        elif self.kind == "choice":
            allowed = [value for value, _ in self.choices]
            if raw not in allowed:
                raise ValueError(f"{self.name} must be one of {allowed}, got {raw!r}")
            return raw
        else:
            raise ValueError(f"unknown parameter kind {self.kind!r}")

        if self.kind in {"int", "float"}:
            if self.minimum is not None and value < self.minimum:
                raise ValueError(f"{self.name} must be >= {self.minimum}")
            if self.maximum is not None and value > self.maximum:
                raise ValueError(f"{self.name} must be <= {self.maximum}")
        return value


@dataclass(frozen=True)
class ParamSet:
    params: tuple[Param, ...] = field(default_factory=tuple)

    def defaults(self) -> dict[str, Any]:
        return {param.name: param.default for param in self.params}

    def resolve(self, raw: dict[str, Any] | None) -> dict[str, Any]:
        """Fill in defaults, drop unknown keys, and validate what remains."""
        supplied = raw or {}
        resolved = {}
        for param in self.params:
            if param.name in supplied and supplied[param.name] is not None:
                resolved[param.name] = param.coerce(supplied[param.name])
            else:
                resolved[param.name] = param.default
        return resolved

    def to_list(self) -> list[dict[str, Any]]:
        return [param.to_dict() for param in self.params]


# Parameters that show up in more than one scenario keep the same name and label,
# so switching demos in class does not change what a control means.
SEED = Param("seed", "随机种子 (seed)", "int", 2026, minimum=0, maximum=10**9,
             help="同一种子重放同一条轨迹；换种子探索更多调度。")


def nodes_param(default: int = 5, minimum: int = 3, maximum: int = 7) -> Param:
    return Param("nodes", "节点数 (nodes)", "int", default, minimum=minimum, maximum=maximum,
                 help="投票成员数量；多数派为 n // 2 + 1。")


DROP = Param("drop", "丢包率 (drop rate)", "float", 0.02, minimum=0.0, maximum=0.9, step=0.01,
             help="每条消息被网络丢弃的概率。")
DELAY_MIN = Param("delay_min", "最小延迟 (min delay)", "int", 4, minimum=1, maximum=200)
DELAY_MAX = Param("delay_max", "最大延迟 (max delay)", "int", 18, minimum=1, maximum=400)
HEARTBEAT = Param("heartbeat", "心跳间隔 (heartbeat)", "int", 40, minimum=5, maximum=400,
                  help="Leader 发送 AppendEntries 的周期。")
TIMEOUT_MIN = Param("timeout_min", "选举超时下界 (election timeout)", "int", 150, minimum=20, maximum=2000)
TIMEOUT_MAX = Param("timeout_max", "选举超时上界 (election timeout)", "int", 300, minimum=20, maximum=4000)
