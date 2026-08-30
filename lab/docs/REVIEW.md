# Distributed Databases Lab Demonstration — Code Review

Review date: 2026-08-30

## Executive verdict

The lab is well designed for instructor-led demonstrations on its default, scripted paths. Its deterministic event kernel, trace-first architecture, deliberate counterexamples, and focused browser UI are unusually strong teaching choices. The default test suite passes, all 17 demonstrations load, and the intended safe/unsafe comparisons generally produce the documented conclusions.

It is **conditionally suitable for classroom use**, with two qualifications:

1. Fix the snapshot/AppendEntries correctness defect below before encouraging students to explore high delay and loss settings.
2. Correct the quorum definition in the existing README before distributing it to students.

The remaining findings are important reliability and documentation improvements, but they do not invalidate the default demonstrations.

## What was verified

- Ran `python3 -m unittest discover -s lab -t lab -p 'test_*.py' -v`: **77/77 tests passed** when local loopback binding was enabled.
- Compiled all Python lab files and syntax-checked all browser JavaScript files.
- Started the loopback HTTP server and exercised all **17** default scenarios in a real browser. Every scenario loaded; the expected default invariant results appeared; no browser console errors were observed.
- Compared command-line, API, and browser results for representative safe and deliberately unsafe configurations.
- Sampled parameter boundaries and pairs. This exposed cross-field validation failures and a snapshot replication defect that the current tests do not cover.
- Confirmed byte-for-byte replay for equal seeds through the existing determinism tests.

Environment used for the review: Python 3.14.2 and the Codex in-app Chromium browser on macOS.

## Critical findings

### C1. A stale AppendEntries can corrupt a follower after snapshot installation

Severity: **Critical correctness issue**  
Affected code: `lab/cluster_lab/nodes/raft.py`, especially `term_at()` (lines 107–114) and `_on_append_entries()` (lines 318–365).

After a follower installs a snapshot with `log_base > 0`, an older in-flight AppendEntries from the same term can still arrive with `prevLogIndex = 0` and `prevLogTerm = 0`. `term_at(0)` returns `0`, so the prefix check accepts the message. The append loop then computes negative list offsets for indices inside the compacted prefix and appends entries whose absolute indices are at or below `log_base`.

Reproduction using only allowed UI/API parameter values:

```bash
python3 lab/run.py run snapshot-catchup \
  --seed 25 \
  --set drop=0.5 \
  --set delay_min=100 \
  --set delay_max=200
```

Observed final state:

- A/B/C/D: commit index 4, state `x=6`.
- E: snapshot base 3 and commit index 4, but its remaining log contains an entry whose own index is 2; its state is `x=5`.
- The lab reports `相同日志得到相同状态` as violated even though the selected protocol is intended to be safe. The actual cause is log corruption, not nondeterministic application.

This matters pedagogically: a student can select a valid network configuration and receive a false lesson that Raft snapshot catch-up or deterministic state machines are unsafe.

Recommended fix:

- Never allow AppendEntries to modify indices at or below `log_base`.
- If `prevLogIndex < log_base`, reject the stale request (or return an explicit compacted-prefix hint) so the leader retries from the snapshot boundary.
- Avoid negative offsets when truncating or appending relative to `log_base`.
- Add a deterministic regression test: deliver an in-flight same-term AppendEntries with `prevLogIndex=0` after installing a snapshot at index 3, then assert that `base`, suffix indices, applied state, and commit position remain coherent.
- Add a structural invariant for every Raft node: suffix entries are contiguous and every suffix entry has `entry.index > log_base`.

### C2. The README gives an incorrect definition of a Raft majority

Severity: **Critical teaching/documentation issue**  
Affected text: `lab/README.md`, line 337.

The README currently says that crashed nodes are not counted in a majority. That is not how Raft quorums work. The majority threshold is based on the configured voting membership; failed or unreachable voters remain in the denominator but cannot contribute votes or acknowledgements. Allowing the threshold to shrink with live nodes would permit disjoint quorums and break safety.

The implementation itself uses the correct fixed membership size, for example `majority(len(self.cluster.node_ids))` in `lab/cluster_lab/nodes/raft.py`.

Recommended replacement:

> 多数派按当前配置中的投票成员总数计算（`n // 2 + 1`）。崩溃或不可达节点仍属于配置，只是不能贡献票或确认；因此故障过多时系统会失去可用性，而不会自动缩小多数派。

Also update the nearby statement that the teaching Raft “omits snapshots”; the same lab contains a snapshot installation demonstration. Describe the snapshot support as simplified instead.

## High-priority findings

### H1. Exposed parameter controls can create invalid pairs and abort a run

Affected code: `lab/cluster_lab/params.py`, `lab/cluster_lab/network.py`, and `lab/cluster_lab/nodes/raft.py`.

Each numeric field is range-checked independently, but related fields are not validated together. The browser therefore allows configurations such as:

- `delay_min=200`, `delay_max=18`, causing `ValueError: delays must satisfy ...`.
- `timeout_min=2000`, `timeout_max=300`, causing an empty `randrange` error.
- `timeout_min=20`, `timeout_max=20`, which is also invalid because the upper bound is exclusive.

The delay issue affects all 12 networked demonstrations. The timeout issue affects Raft election and the normal replication mode. The server returns a JSON error rather than crashing, but the classroom run is interrupted and the raw error is not actionable for students.

Recommended fix:

- Add ParamSet-level validators for `delay_min <= delay_max` and `timeout_min < timeout_max`.
- In the browser, dynamically constrain the paired slider or show an inline validation message before running.
- Add boundary and pairwise tests through both `server.run_scenario()` and the HTTP API.
- For a clearer Raft demonstration, warn when the heartbeat interval is not comfortably below the election timeout; it may be legal as an experiment, but it changes the lesson from normal operation to election instability.

### H2. The trace contract test does not enforce what its name and README claim

Affected test: `lab/tests/test_lab.py`, `test_every_message_is_delivered_or_dropped_exactly_once`.

The test verifies that terminal message IDs are unique and are a subset of sent IDs. It does **not** assert that every sent message has a terminal `msg.deliver` or `msg.drop` event. Five default scenarios currently finish with 2–4 messages still in flight, usually a heartbeat sent exactly at the scenario horizon.

This is not a safety failure by itself, but it contradicts both the test name and the stated trace contract. It also means final metrics and visuals may represent a cut with unresolved traffic while the UI looks finished.

Recommended fix: choose and document one contract.

- If traces must be closed, stop recurring timers before the horizon and drain scheduled deliveries, or emit `msg.drop` with a cause such as `simulation_end`.
- If pending messages are legitimate, add an explicit `pending` outcome/state, change the test and README wording, and show the count in final metrics.

### H3. “Not checked” is encoded and rendered as “passed”

Affected code: `lab/cluster_lab/invariants.py` (lines 154–161) and `lab/web/app.js` (`renderChecks`).

When a single-key history exceeds the 14-operation exhaustive-search limit, the checker returns `ok=True` while its detail says “this is not a pass; it was not checked.” The UI renders every `ok=True` result as a green `通过`, and the summary excludes it from violations.

Current scenario maxima are deliberately kept at or below 14, so the default demonstrations do not hit this path. The representation is still unsafe for future exercises and API consumers.

Recommended fix: replace the boolean result with a three-state status such as `pass`, `fail`, and `skipped/inconclusive`, and render inconclusive checks neutrally. A bounded checker result must never be indistinguishable from a proof obligation that passed.

## Medium- and low-priority findings

### M1. The acknowledgement demonstration advertises a history view but does not expose it

`ack-meaning` is registered with `views=("cluster", "history")`, but its call to the shared `build()` helper uses the helper default, so the emitted trace contains only `cluster`. The browser consequently displays only the cluster tab.

Recommended fix: pass `views=("cluster", "history")` in `s00_ack_meaning.py` and add a test asserting that every scenario's declared views equal its trace views. The missing history is especially useful here because `unknown`, retries, and reused request IDs are central to the lesson.

### M2. The instructor script contains stale exact figures

The table in `lab/README.md` for `order-vs-coordinate` no longer matches seed 2026 and the current defaults:

| Metric | README | Current output |
|---|---:|---:|
| Uncoordinated final total | 3922 / 4000 | 3942 / 4000 |
| 2PC execution-time cross-shard messages | 101 | 113 |

The qualitative conclusion remains correct. Update the figures, or make the script instruct the lecturer to read the live metrics rather than memorise exact numbers. A regression test should cover any exact number that remains in course material.

### M3. “Pause on teaching points” does not pause on ordinary `teach` notes

The README and control label imply that all teaching notes cause a pause, but `NOTE_PAUSE_LEVELS` contains only `phase`, `warn`, and `violation`. Notes with level `teach` appear without stopping playback.

Either include `teach` in the pause set or rename the control and documentation to “关键阶段/警告暂停”. The current behavior may be a reasonable pacing choice; the issue is the mismatch.

### M4. Cross-component contracts need more browser-level coverage

The Python tests are strong, but there is no automated assertion for:

- scenario registry views versus emitted trace views;
- browser rendering of pass/fail/inconclusive states;
- paired parameter validation;
- switching every visualization tab;
- export filenames and SVG/JSON content;
- projector mode and keyboard controls.

A small headless browser smoke suite would catch the missing history tab and UI contract drift without requiring pixel-perfect snapshots.

### L1. Runtime prerequisites are underspecified

The lab uses Python syntax that requires Python 3.10 or newer, but the README only says “Python standard library.” State the minimum version, give a preflight command, and document how to select a different port if 8000 is occupied.

### L2. A few README inventory statements are stale

The directory listing mentions `lab/PLAN.md`, which is not present, and the boundary section says snapshots are omitted even though `snapshot-catchup` is a core demonstration. These are low-risk but easy to correct.

## Strengths worth preserving

- **One source of truth:** Python produces the trace; the browser only replays it. This keeps CLI, tests, exported evidence, and animation aligned.
- **Deterministic scheduling:** logical ticks, one seeded RNG, stable routing, and no wall-clock sleeps make classroom reproduction dependable.
- **Counterexamples are first-class:** unsafe settings are expected to fail and point back to evidence rather than merely showing an alarm.
- **Good conceptual separation:** `ok`, `unknown`, and `fail` are distinct; safety and liveness are separated in the 2PC demonstration; “no counterexample found” is not presented as proof in the Jepsen loop.
- **Explicit model caveats:** AP, ARIES, query costs, and product architecture paths are identified as teaching models rather than production implementations or benchmarks.
- **Useful classroom UX:** prediction prompts, step controls, projection mode, trace export, invariant cards, and deterministic seed sweeps support a coherent teaching routine.
- **Broad test intent:** the suite includes positive cases, required counterexamples, checker agreement, determinism, routing stability, and recovery behavior.

## Recommended remediation order

1. Fix C1 and add the stale-AppendEntries-after-snapshot regression test.
2. Correct C2 before distributing the handout.
3. Add paired parameter validation (H1).
4. Decide and enforce the terminal-message trace contract (H2).
5. Add an inconclusive check state (H3).
6. Restore the missing acknowledgement history view and add declared-view contract tests.
7. Refresh exact instructor-script figures and prerequisites.
8. Add a compact browser smoke suite for scenario/view/controls/export coverage.

After items 1–3, the lab should be dependable for normal classroom use, including guided parameter changes. After items 4–8, it will also be much safer as an open-ended student exploration tool.
