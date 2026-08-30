"""第6讲：ARIES 恢复 — 先重复历史，再撤销失败者。

工作负载是固定的九步，崩溃点可以停在任何一步。两条落盘先后规则各有开关：
关掉哪一条，日志就在哪一处失去解释磁盘的能力。
"""

from __future__ import annotations

from typing import Any

from ..invariants import run_checks
from ..kernel import Kernel
from ..nodes.storage import (ACK_THEN_FSYNC, COMMIT, FSYNC_THEN_ACK, NO_WAL, WAL, Storage)
from ..params import SEED, Param, ParamSet
from ..registry import Prediction, RunResult, Scenario, register
from ..trace import Trace

STEPS = [
    ("T1 更新 a=10", lambda s: s.update("T1", "a", 10)),
    ("T1 更新 b=20", lambda s: s.update("T1", "b", 20)),
    ("T1 提交", lambda s: s.commit("T1")),
    ("写检查点", lambda s: s.checkpoint()),
    ("T2 更新 c=30", lambda s: s.update("T2", "c", 30)),
    ("刷出页面 P1", lambda s: s.flush_page("P1")),
    ("T3 更新 d=40", lambda s: s.update("T3", "d", 40)),
    ("T3 提交", lambda s: s.commit("T3")),
    ("刷出页面 P2", lambda s: s.flush_page("P2")),
]

PARAMS = ParamSet((
    Param("crash_at_step", "在第几步之后崩溃 (crash after step)", "int", 6,
          minimum=1, maximum=len(STEPS),
          help="第 6 步刚刷出 P1，第 8 步刚确认 T3 提交。", axis=True),
    Param("wal_policy", "落盘先后规则一 (WAL)", "choice", WAL, choices=(
        (WAL, "刷页前先把对应日志刷盘"),
        (NO_WAL, "直接刷页，不管日志（违反 WAL）"),
    ), axis=True),
    Param("commit_policy", "落盘先后规则二 (commit durability)", "choice", FSYNC_THEN_ACK,
          choices=(
              (FSYNC_THEN_ACK, "提交记录落盘后再回复客户端"),
              (ACK_THEN_FSYNC, "先回复客户端，日志稍后再刷（违反规则）"),
          ), axis=True),
    Param("crash_during_recovery", "恢复途中再次崩溃", "choice", "off", choices=(
        ("off", "不再崩溃"),
        ("on", "撤销到一半再崩一次"),
    )),
    Param("write_clr", "写补偿日志记录 (CLR)", "choice", "on", choices=(
        ("on", "每次撤销都写 CLR"),
        ("off", "不写 CLR"),
    ), help="只影响恢复途中再次崩溃时要重做多少撤销工作。"),
    SEED,
))

END = 1200


def run(params: dict[str, Any], seed: int) -> RunResult:
    trace = Trace("aries-recovery", seed, params, ["storage"], title="ARIES 恢复")
    kernel = Kernel(seed)
    for page in ("P1", "P2"):
        trace.add_node(page, kind="page")
    storage = Storage(kernel, trace, wal_policy=params["wal_policy"],
                      commit_policy=params["commit_policy"],
                      write_clr=params["write_clr"] == "on")

    trace.note(0,
               "WAL 有两条先后规则：描述页面更新的日志必须先于该页面落盘；"
               "提交记录必须先于“提交成功”的回复落盘。"
               "工作负载固定，崩溃点可调——看日志在哪一步失去解释磁盘的能力。",
               level="phase", title="设定：两条先后规则")
    if params["wal_policy"] == NO_WAL:
        trace.note(0, "本次关闭了第一条规则：页面可以先于它的日志记录落盘。",
                   level="warn", title="违反 WAL")
    if params["commit_policy"] == ACK_THEN_FSYNC:
        trace.note(0, "本次关闭了第二条规则：提交记录还没落盘就回复客户端成功。",
                   level="warn", title="提交先回复后落盘")

    stop = params["crash_at_step"]
    for number, (label, action) in enumerate(STEPS[:stop], start=1):
        at = number * 60
        kernel.schedule_at(at, lambda action=action: action(storage))
        kernel.schedule_at(at, lambda label=label, number=number: trace.record(
            kernel.now, "workload.step", step=number, label=label))
    kernel.run(stop * 60 + 30)

    acked = list(storage.acked)
    crash_at = stop * 60 + 60
    kernel.schedule_at(crash_at, lambda: (
        storage.crash(),
        trace.note(kernel.now,
                   f"崩溃。日志持久化到 LSN {storage.durable_lsn}，"
                   "尚未落盘的日志尾部消失了；磁盘上的页面停在各自最后一次刷出的样子。",
                   level="phase", title="崩溃"),
    ))
    kernel.run(crash_at + 10)

    surviving = [record.txn for record in storage.log if record.kind == COMMIT]
    expected = storage.expected()

    def do_recover() -> None:
        trace.note(kernel.now,
                   "Analysis 重建现场：谁是 winner、谁是 loser、哪些页面可能脏。"
                   "接着 Redo 重演历史——包括 loser 的更新——最后再统一撤销 loser。",
                   level="teach", title="恢复开始")
        if params["crash_during_recovery"] == "on":
            finished = storage.recover(stop_after_undo=1)
            if not finished:
                trace.note(kernel.now,
                           "撤销到一半又崩了一次。"
                           + ("已写下的 CLR 记录了补偿到哪一步，第二次恢复可以接着往下走。"
                              if params["write_clr"] == "on"
                              else "没有 CLR，第二次恢复只能从头把撤销重做一遍。"),
                           level="warn", title="恢复途中再次崩溃")
                storage.crash()
                storage.recover()
        else:
            storage.recover()

    kernel.schedule_at(crash_at + 80, do_recover)

    def verdict() -> None:
        recovered = storage.state()
        listing = "，".join(f"{key}={value}" for key, value in sorted(recovered.items()))
        want = "，".join(f"{key}={value}" for key, value in sorted(expected.items()))
        if recovered == expected:
            trace.note(kernel.now,
                       f"恢复结果 {listing}，与预期一致（{want}）。"
                       "winner 的效果都在，loser 的效果一点都不剩。",
                       level="teach", title="对账")
        else:
            trace.note(kernel.now,
                       f"恢复结果 {listing}，预期是 {want}。"
                       "差异不是恢复算法算错了，而是它拿到的日志本来就解释不了磁盘上的内容。",
                       level="violation", title="日志解释不了磁盘")

    kernel.schedule_at(END - 60, verdict)
    kernel.run(END)

    trace.facts["expected"] = expected
    trace.facts["recovered"] = storage.state()
    trace.facts["acked_commits"] = acked
    trace.facts["surviving_commits"] = surviving
    trace.facts["disk"] = {page: dict(state) for page, state in storage.disk.items()}

    checks = run_checks(trace, ("recovery_correct", "durable_commits"))
    metrics = {
        "崩溃点": f"第 {stop} 步（{STEPS[stop - 1][0]}）",
        "崩溃时日志持久化到": f"LSN {storage.durable_lsn}",
        "已向客户端确认的提交": "、".join(acked) or "无",
        "崩溃后仍有提交记录的事务": "、".join(surviving) or "无",
        "Redo 应用 / 跳过": f"{storage.redo_applied} / {storage.redo_skipped}",
        "撤销步数 (undo steps)": storage.undo_steps,
        "恢复结果": "，".join(f"{key}={value}" for key, value in sorted(storage.state().items())),
    }
    return RunResult(trace, checks, metrics)


register(Scenario(
    id="aries-recovery",
    order=61,
    lecture="第6讲 快照与恢复",
    title="ARIES 恢复",
    subtitle="Analysis、Redo、Undo 与两条落盘先后规则",
    views=("storage",),
    params=PARAMS,
    run=run,
    question="崩溃之后，哪些更新需要重做，哪些需要撤销？如果页面先于它的日志落盘，会发生什么？",
    takeaway="ARIES 先重演历史（包括 loser 的更新），把数据库恢复到崩溃瞬间的样子，"
             "再统一逻辑撤销失败者并写下 CLR。"
             "它的前提是日志仍然能解释磁盘：页面必须后于它的日志落盘，"
             "提交记录必须先于“提交成功”的回复落盘。关掉任何一条，恢复算法本身再正确也没用。",
    prediction=Prediction(
        question="一个页面带着尚未落盘的日志记录被写到磁盘，然后系统崩溃。恢复会怎样？",
        options=("Redo 会把它补回来", "Undo 会把它撤销掉",
                 "恢复根本不知道有过这次更新，未提交的修改留在了磁盘上", "校验和会发现问题"),
        answer=2,
        why="恢复只能依据持久化的日志。那条描述更新的记录没有落盘，"
            "分析阶段就看不到这个事务，也就不会撤销它——未提交的修改从此留在数据里。",
    ),
    invariants=("recovery_correct", "durable_commits"),
    faults=("crash", "crash_during_recovery", "no_wal", "ack_before_fsync"),
    caveat="页面与键都是教学模型（两页四键，物理 undo）；重点在两条先后规则与三个恢复阶段。",
))
