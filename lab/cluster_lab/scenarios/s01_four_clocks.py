"""第1讲：四种“时间”记录同一段执行 — 每种能证明什么，不能证明什么。

事件脚本是固定的，因果关系因此完全已知。我们把真实的 happened-before 关系
当作标准答案，再检查用某种时钟做出的判断对不对：

- 物理时钟带偏差时会把因果顺序倒过来；
- Lamport 保住了因果方向，却把并发事件排成了先后；
- 向量时钟两项都对，代价是元数据随参与者增长；
- HLC 跟随墙上时钟且保住因果方向，但它是标量，同样识别不了并发。
"""

from __future__ import annotations

from typing import Any

from examples.lamport_clock import Process

from ..kernel import Kernel
from ..params import SEED, Param, ParamSet
from ..registry import CheckResult, Prediction, RunResult, Scenario, register
from ..trace import Trace

PROCESSES = ["P1", "P2", "P3"]
SKEW = {"P1": 0, "P2": 90, "P3": -60}

# (process, tick, kind, name, target, arrival)
SCRIPT = [
    ("P1", 10, "local", "a1", None, None),
    ("P1", 30, "send", "a→", "P2", 70),
    ("P3", 45, "local", "c1", None, None),
    ("P2", 70, "recv", "b1", None, None),
    ("P2", 95, "local", "b2", None, None),
    ("P2", 110, "send", "b→", "P3", 150),
    ("P1", 130, "local", "a2", None, None),
    ("P3", 150, "recv", "c2", None, None),
    ("P3", 175, "send", "c→", "P1", 215),
    ("P1", 215, "recv", "a3", None, None),
]

PARAMS = ParamSet((
    Param("clock", "用哪种时钟判断 (clock)", "choice", "physical", choices=(
        ("physical", "物理时钟 physical"),
        ("lamport", "Lamport 逻辑时钟"),
        ("vector", "向量时钟 vector"),
        ("hlc", "混合逻辑时钟 HLC"),
    ), axis=True),
    Param("skew", "时钟偏差 (clock skew)", "choice", "on", choices=(
        ("on", "各进程时钟有偏差"),
        ("off", "各进程时钟完全同步"),
    ), help="只影响物理时钟与 HLC 的读数。", axis=True),
    Param("_seed_note", "说明", "choice", "fixed", choices=(("fixed", "本演示是固定脚本"),),
          help="事件顺序不随种子变化，因果关系因此可以作为标准答案。"),
    SEED,
))

END = 260


def _vector_receive(local: list[int], remote: list[int], me: int) -> list[int]:
    """课件第1讲给出的向量时钟接收规则：逐分量取最大，再把本进程分量加一。"""
    merged = [max(left, right) for left, right in zip(local, remote)]
    merged[me] += 1
    return merged


def _hlc_step(state: tuple[int, int], physical: int,
              remote: tuple[int, int] | None) -> tuple[int, int]:
    logical, counter = state
    candidates = [logical, physical] + ([remote[0]] if remote else [])
    highest = max(candidates)
    if highest == logical and (not remote or highest > remote[0]):
        counter += 1
    elif remote and highest == remote[0] and highest == logical:
        counter = max(counter, remote[1]) + 1
    elif remote and highest == remote[0]:
        counter = remote[1] + 1
    elif highest == logical:
        counter += 1
    else:
        counter = 0
    return (highest, counter)


def _build(skewed: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    offsets = SKEW if skewed else {name: 0 for name in PROCESSES}
    lamport = {name: Process(name) for name in PROCESSES}
    vector = {name: [0, 0, 0] for name in PROCESSES}
    hlc = {name: (0, 0) for name in PROCESSES}

    events: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    in_flight: dict[str, dict[str, Any]] = {}
    previous: dict[str, int] = {}
    pending_recv: dict[str, str] = {}

    for proc, tick, kind, name, target, arrival in SCRIPT:
        index = PROCESSES.index(proc)
        physical = tick + offsets[proc]
        carried = in_flight.pop(pending_recv.pop(proc, ""), None) if kind == "recv" else None

        if kind == "recv" and carried is not None:
            stamp_l = lamport[proc].receive(carried["lamport"])
            vector[proc] = _vector_receive(vector[proc], carried["vector"], index)
            hlc[proc] = _hlc_step(hlc[proc], physical, carried["hlc"])
        else:
            stamp_l = lamport[proc].send() if kind == "send" else lamport[proc].local_event()
            vector[proc] = list(vector[proc])
            vector[proc][index] += 1
            hlc[proc] = _hlc_step(hlc[proc], physical, None)

        event = {
            "id": f"e{len(events) + 1}", "proc": proc, "at": tick, "kind": kind, "name": name,
            "physical": physical, "lamport": stamp_l, "vector": list(vector[proc]),
            "hlc": list(hlc[proc]), "prev": previous.get(proc),
            "cause": carried["event"] if carried else None,
        }
        previous[proc] = len(events)
        events.append(event)

        if kind == "send":
            identifier = f"m{len(messages) + 1}"
            in_flight[identifier] = {"event": len(events) - 1, "lamport": stamp_l,
                                     "vector": list(vector[proc]), "hlc": hlc[proc]}
            pending_recv[target] = identifier
            messages.append({"id": identifier, "from": proc, "to": target,
                             "sendAt": tick, "recvAt": arrival, "sender": len(events) - 1})
    return events, messages


def _happened_before(events: list[dict[str, Any]]) -> list[set[int]]:
    """Transitive closure of program order plus message order."""
    reaches: list[set[int]] = [set() for _ in events]
    for index in range(len(events) - 1, -1, -1):
        for successor in (events[index].get("_next") or []):
            reaches[index] |= {successor} | reaches[successor]
    return reaches


def _link(events: list[dict[str, Any]], messages: list[dict[str, Any]]) -> None:
    for index, event in enumerate(events):
        event["_next"] = []
    order: dict[str, list[int]] = {}
    for index, event in enumerate(events):
        order.setdefault(event["proc"], []).append(index)
    for indexes in order.values():
        for first, second in zip(indexes, indexes[1:]):
            events[first]["_next"].append(second)
    for index, event in enumerate(events):
        if event["cause"] is not None:
            events[event["cause"]]["_next"].append(index)


def _compare(clock: str, first: dict[str, Any], second: dict[str, Any]) -> str:
    if clock == "vector":
        left, right = first["vector"], second["vector"]
        if left == right:
            return "concurrent"
        if all(a <= b for a, b in zip(left, right)):
            return "before"
        if all(a >= b for a, b in zip(left, right)):
            return "after"
        return "concurrent"
    key = {"physical": "physical", "lamport": "lamport", "hlc": "hlc"}[clock]
    left, right = first[key], second[key]
    if isinstance(left, list):
        left, right = tuple(left), tuple(right)
    if left < right:
        return "before"
    if left > right:
        return "after"
    return "concurrent"


def run(params: dict[str, Any], seed: int) -> RunResult:
    clock = params["clock"]
    events, messages = _build(params["skew"] == "on")
    _link(events, messages)
    reaches = _happened_before(events)

    trace = Trace("four-clocks", seed, params, ["timeline"], title="四种时间")
    kernel = Kernel(seed)
    for name in PROCESSES:
        trace.add_node(name, kind="process")
    trace.facts["events"] = events
    trace.facts["processes"] = [
        {"id": name, "offset": (SKEW if params["skew"] == "on" else {}).get(name, 0)}
        for name in PROCESSES
    ]

    trace.note(0,
               "同一段执行被四种时钟同时记录。事件脚本固定，因此真实的 happened-before "
               "关系是已知的——我们拿它当标准答案，检查用这种时钟做出的判断对不对。",
               level="phase", title="设定：标准答案已知")

    timeline = sorted(
        [(event["at"], "event", index) for index, event in enumerate(events)] +
        [(message["sendAt"], "send", position) for position, message in enumerate(messages)] +
        [(message["recvAt"], "deliver", position) for position, message in enumerate(messages)]
    )
    for at, kind, position in timeline:
        if kind == "event":
            event = events[position]
            trace.record(at, "tl.event", id=event["id"], proc=event["proc"], name=event["name"],
                         eventKind=event["kind"], physical=event["physical"],
                         lamport=event["lamport"], vector=event["vector"], hlc=event["hlc"])
        elif kind == "send":
            message = messages[position]
            trace.record(at, "msg.send", id=message["id"], **{"from": message["from"]},
                         to=message["to"], kind="消息", label=message["id"],
                         eta=message["recvAt"])
        else:
            trace.record(at, "msg.deliver", id=messages[position]["id"])

    # -- the two questions ---------------------------------------------------------

    causal_pairs = [(a, b) for a in range(len(events)) for b in reaches[a]]
    concurrent_pairs = [
        (a, b) for a in range(len(events)) for b in range(a + 1, len(events))
        if b not in reaches[a] and a not in reaches[b]
    ]

    wrong_causal = [(a, b) for a, b in causal_pairs
                    if _compare(clock, events[a], events[b]) != "before"]
    wrong_concurrent = [(a, b) for a, b in concurrent_pairs
                        if _compare(clock, events[a], events[b]) != "concurrent"]

    def evidence(pair: tuple[int, int]) -> list[dict[str, Any]]:
        return [{"t": events[index]["at"], "id": events[index]["id"],
                 "name": events[index]["name"]} for index in pair]

    def describe(pair: tuple[int, int]) -> str:
        first, second = events[pair[0]], events[pair[1]]
        show = {"physical": "physical", "lamport": "lamport",
                "vector": "vector", "hlc": "hlc"}[clock]
        return (f"{first['name']}({first[show]}) 与 {second['name']}({second[show]})")

    checks = [
        CheckResult(
            "因果顺序被保留 (causality preserved)", not wrong_causal,
            f"{len(causal_pairs)} 对有因果关系的事件全部被正确排序。" if not wrong_causal
            else f"{len(wrong_causal)} 对因果关系被弄反或抹平，例如 {describe(wrong_causal[0])}"
                 "——真实关系是前者导致后者。",
            evidence(wrong_causal[0]) if wrong_causal else [],
        ),
        CheckResult(
            "并发判定正确 (concurrency inference)", not wrong_concurrent,
            f"{len(concurrent_pairs)} 对并发事件都被正确判定为并发。" if not wrong_concurrent
            else f"{len(wrong_concurrent)} 对真正并发的事件被判成了先后，例如 "
                 f"{describe(wrong_concurrent[0])}。标量时间戳给出全序，"
                 "无法区分“真实因果”与“恰好数值不同”。",
            evidence(wrong_concurrent[0]) if wrong_concurrent else [],
        ),
    ]

    verdicts = {
        "physical": "物理时钟适合记录“大约何时”，不适合独自证明因果。",
        "lamport": "Lamport 保证 a→b ⇒ C(a)<C(b)，反过来不成立：它排得出顺序，判不出并发。",
        "vector": "向量时钟与因果关系等价，代价是元数据随参与者数量增长。",
        "hlc": "HLC 跟随墙上时钟又保住因果方向，便于按时间查询；但它仍是标量，识别不了并发。",
    }
    if clock == "physical" and params["skew"] == "off":
        trace.note(END - 40,
                   "这一次因果顺序没有被弄反——因为我们假设所有时钟完全同步。"
                   "但同步是一个假设，不是事实：石英钟会漂移，NTP 会调速，"
                   "网络延迟不对称使单次测量也难以得到精确偏差。把偏差打开再看一次。",
                   level="warn", title="同步是假设，不是事实")
    trace.note(END - 20, verdicts[clock], level="teach", title=f"{clock} 的能力边界")
    trace.record(END, "metric", name="clock", value=clock)

    metrics = {
        "时钟 (clock)": clock,
        "因果对数 (causal pairs)": len(causal_pairs),
        "并发对数 (concurrent pairs)": len(concurrent_pairs),
        "因果判断错误": len(wrong_causal),
        "并发判断错误": len(wrong_concurrent),
        "每个事件的元数据": {"physical": "1 个整数", "lamport": "1 个整数",
                       "vector": f"{len(PROCESSES)} 个整数", "hlc": "2 个整数"}[clock],
    }
    return RunResult(trace, checks, metrics)


register(Scenario(
    id="four-clocks",
    order=10,
    lecture="第1讲 时间与日志",
    title="四种时间",
    subtitle="物理时钟、Lamport、向量时钟与 HLC",
    views=("timeline",),
    params=PARAMS,
    run=run,
    question="同一段执行，用四种时间分别标注。每一种能证明什么，不能证明什么？",
    takeaway="顺序不是时钟读数，而是协议产物。物理时钟带偏差会倒置因果；"
             "Lamport 保住因果方向但把并发排成先后；向量时钟与因果等价，代价是元数据；"
             "HLC 兼顾墙上时钟与因果，但仍是标量。选哪种，取决于你要证明什么。",
    prediction=Prediction(
        question="Lamport 时间戳满足 C(a) < C(b)。可以断定 a 导致了 b 吗？",
        options=("可以，这就是 Lamport 时钟的用途", "不可以，它们可能只是并发",
                 "只有同一进程内可以", "只有跨进程时可以"),
        answer=1,
        why="Lamport 只保证 a→b ⇒ C(a)<C(b)，反过来不成立。"
            "两个无关进程的计数恰好不同，也会得到一个先后关系。"
            "要得到与因果等价的比较，需要向量时钟这类多维信息。",
    ),
    invariants=("因果顺序被保留", "并发判定正确"),
    faults=("clock_skew",),
    caveat="本演示是固定事件脚本：因果关系完全已知，因此可以把它当作检查时钟判断的标准答案。",
))
