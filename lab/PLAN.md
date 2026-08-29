# 课堂集群实验室（cluster-lab）实施计划

> 状态：**草案，待审阅**。本文件描述一个贯穿全课程的分布式系统演示工具。
> 审阅通过后按“分期交付”一节实施；本文件随实现更新，最终可并入 `lab/README.md`。

## 1. 目标

为《分布式数据库开发》提供**一套**演示工具，同时满足：

- **绪论可用**：第一节课就能用它把“确认、分区、失败不是异常分支”演出来。
- **后续每讲可用**：时间、共识、分片、事务、查询、恢复、验证、架构对比各有对应演示。
- **教师可控**：菜单选择演示、表单配置参数、可暂停/单步/回放/对比。
- **学生可读**：行为由 Python 脚本定义，学生读的就是课件里出现的那些规则。
- **零安装**：只用 Python 标准库 + 浏览器；不引入 pip 依赖，不需要 Node 参与运行。

一句话主张（借用第9讲的“立字据”要求）：

> 同一个确定性模拟内核，用固定种子产生一条完整事件轨迹；网页只负责回放这条轨迹，
> 因此**课堂看到的动画与实验报告里的原始数据是同一份证据**。

## 2. 设计原则（直接来自课件约定）

| 课件约定 | 本工具的落实方式 |
|---|---|
| 随机故障、延迟、调度必须显式设置种子 | 每次运行只有一个 `Random(seed)`；种子写进轨迹文件与界面 |
| 模拟优先用逻辑事件队列，而非真实 `sleep` | 内核是整数逻辑时钟 + 最小堆；动画速度只是回放倍率 |
| 不用 Python 内置 `hash()` 做分片 | 复用 `examples/sharding.py` 的 blake2b 稳定散列 |
| 超时结果必须保留为 `unknown` | 客户端历史三态 `ok / fail / info`，界面用三种颜色 |
| 结论要能追到一次运行、一个检查规则、一段实现 | 轨迹 JSON 含 scenario/seed/params/版本；检查器输出最小反例 |
| 固定种子不等于覆盖充分 | 内置“种子扫描”按钮：批量跑 N 个种子，列出所有反例种子 |
| 不把模拟结果包装成真实部署结论 | 架构对比场景显式标注“教学模型，非产品基准” |

**关键架构决定：Python 是唯一的真相来源。**
浏览器里没有任何协议逻辑，只有一个轨迹播放器。这样才能保证课堂动画、命令行运行、
单元测试、实验报告四者看到的是同一件事；也才能支持任意回退、单步和对比。

## 3. 系统结构

```
浏览器 (纯回放)              Python (唯一真相来源)
┌─────────────────┐         ┌──────────────────────────────┐
│ 演示菜单         │  GET    │ registry: 场景注册表          │
│ 参数表单         │ ──────► │   每个场景声明:               │
│ 播放器/时间轴    │         │     参数模式 / 支持的故障      │
│ 舞台(6 种视图)   │  POST   │     不变式 / 提问 / 结论      │
│ 事件日志         │ ──────► │ kernel: 逻辑事件队列          │
│ 不变式面板       │ ◄────── │ nodes:  raft/kv/txn/储存/查询 │
│ 指标条           │  轨迹    │ checkers: 线性一致/银行/隔离  │
└─────────────────┘  JSON   └──────────────────────────────┘
```

HTTP 接口（`http.server`，仅绑定 `127.0.0.1`）：

- `GET  /api/scenarios` → 菜单元数据（分组按讲次）+ 参数模式 + 提问 + 教学要点
- `POST /api/run` → `{scenario, params, seed}` 返回一条完整轨迹
- `POST /api/compare` → 沿场景声明的 `compare_axis` 跑两组配置，返回两条轨迹 + 指标差
- `POST /api/sweep` → 批量跑 `seed_from..seed_to`，返回违规种子列表（不返回完整轨迹）
- `GET  /` 及静态资源 → `lab/web/`

## 4. 轨迹格式（Python 与前端的唯一契约）

```jsonc
{
  "schema": 1,
  "scenario": "raft-replication",
  "seed": 2026,
  "params": { "nodes": 5, "drop": 0.1, "commit_rule": "current_term" },
  "views": ["cluster"],
  "nodes":   [ { "id": "A", "kind": "replica", "group": "shard-1" } ],
  "clients": [ { "id": "c1" } ],
  "events": [
    { "t": 12, "seq": 2, "type": "msg.send",   "id": "m17", "from": "A", "to": "B",
      "kind": "AppendEntries", "label": "idx4 term3", "eta": 17 },
    { "t": 17, "seq": 3, "type": "msg.deliver","id": "m17" },
    { "t": 17, "seq": 4, "type": "msg.drop",   "id": "m18", "cause": "partition" },
    { "t": 18, "seq": 5, "type": "node.state", "node": "A", "state": "leader", "term": 3 },
    { "t": 18, "seq": 6, "type": "log.append", "node": "A", "index": 4, "term": 3,
      "cmd": "set x=1", "reqId": "req-7" },
    { "t": 20, "seq": 7, "type": "log.commit", "node": "A", "index": 4 },
    { "t": 21, "seq": 8, "type": "client.call","client": "c1", "reqId": "req-7", "op": "write x=1" },
    { "t": 30, "seq": 9, "type": "client.return", "reqId": "req-7", "status": "info" },
    { "t": 31, "seq":10, "type": "fault", "kind": "partition", "groups": [["A","B"],["C","D","E"]] },
    { "t": 31, "seq":11, "type": "note", "level": "teach", "ref": "第2讲",
      "text": "A 仍自认为 Leader，但已无法凑齐多数派：它可以继续接收请求，却不能提交。" }
  ],
  "summary": {
    "metrics": { "committed": 8, "unknown": 1, "leaderChanges": 2, "msgs": 214, "p95Ticks": 43 },
    "checks":  [ { "name": "已确认写不丢失", "ok": true },
                 { "name": "线性一致", "ok": false, "counterexample": [ /* 最小历史 */ ] } ]
  }
}
```

事件分为**通用类**（`note / fault / msg.* / node.state / client.* / metric / check`）与
**视图扩展类**（`log.* / shard.* / exchange / wal.* / page.* / txn.*`）。
新增视图只需新增扩展事件，播放器内核不变。`(t, seq)` 保证全序，与
`examples/deterministic_network.py` 的排序规则一致。

## 5. 视图（舞台的 6 种渲染方式）

| 视图 | 画面 | 服务的场景 |
|---|---|---|
| `cluster` | 节点环 + 消息小球沿边飞行 + 每节点日志条 + 角色/任期徽章 | 共识、复制、分区、2PC、读路径 |
| `timeline` | 进程泳道 + 消息斜箭头（时空图） | Lamport / 向量时钟 / HLC / 因果 |
| `shards` | 键 → 分片路由动画 + 每分片负载柱状图 | 分片策略、热点、分裂迁移 |
| `dataflow` | 算子 DAG + 交换边上的字节流动 | Join 策略、下推、两阶段聚合 |
| `storage` | WAL 条带 + 缓冲页 + LSN 标记 + 崩溃点 | ARIES、检查点、PITR |
| `history` | 客户端操作条（ok 绿 / fail 红 / info 灰）+ 检查器判定 | 写偏差、线性一致检查、Jepsen |

`cluster` 是主视图，最接近 Raft 官方小球动画；其余五个共用同一套播放器、时间轴与配色。
一个场景可以声明多个视图，界面上分标签页（例如 `history` + `cluster` 同屏对照）。

## 6. 演示目录（按讲次）

> 每个场景都声明：`params`（生成表单）、`faults`（可注入故障）、`invariants`（检查器）、
> `prediction`（课堂先猜后验）、`takeaway`（结论）。

### 绪论
- **`ack-meaning` 确认是何意味** — 三副本计数器；客户端超时重试；Leader 在复制前/后崩溃。
  对比轴：`ack = local | majority`；`retry = new_id | same_id`。
  不变式：同一 `request_id` 至多生效一次；已确认写不丢失。
  *直接实现 00 讲“第一个实验”那一页。*
- **`cap-partition` 分区期间必须选择如何失败** — 同一集群注入分区，切换
  `policy = majority_only | any_node | read_snapshot`；AP 模式下让线性一致检查器给出反例。
  *绪论的主力演示，也是第7讲的引子。*

### 第1讲 时间与日志
- **`four-clocks` 四种时间的对比** — 同一组事件分别用物理时钟（带偏差）、Lamport、
  向量时钟、HLC 标注；展示物理时间戳倒置因果、Lamport 无法识别并发。（`timeline`）
- **`nondeterministic-apply` 不可重放的命令** — 三副本收到完全相同的日志，
  `apply()` 里调用随机数/墙上时间 → 状态哈希发散变红；改为命令内携带决定后收敛。

### 第2讲 复制与共识
- **`raft-election` 选举** — 随机超时、投票、分裂选举、任期递增。参数：节点数、
  超时区间、丢包率、种子。（主打小球动画）
- **`raft-replication` 日志复制与提交** — `nextIndex` 回退、前缀修复、提交推进。
  对比轴：`commit_rule = current_term | naive_majority`；后者复现论文 Figure 8，
  让**已提交条目被覆盖**，正面印证 `errata.md` 里那条订正。
- **`read-path` Leader 读也要证明领导权** — `leader_only | readindex | lease | follower_read`
  四种读路径，在分区/时钟跳变下比较陈旧读与等待代价。

### 第3讲 分区与分片
- **`shard-strategies` 分片策略对比** — `range | hash | composite | salted`，倾斜负载
  （复用 `examples/sharding.py` 的 70% 热键工作负载），输出最大/平均分片负载比。（`shards`）
- **`rebalance-routing` 分裂、迁移与路由失效** — 客户端缓存过期 → `RangeMoved` → 刷新重试；
  展示“路由错误是协议内可恢复状态”。

### 第4讲 事务与并发
- **`write-skew` 写偏差与四种修复** — `si | ssi | guard_row | lock`。（`history`）
- **`two-phase-commit` prepared 状态为何不能自行超时** — 协调者在写决议后崩溃，
  参与者阻塞；恢复后读取持久决议继续。对比轴：协调者日志 `single | replicated`
  ——用来说明“用了 Raft ≠ 非阻塞”。
- **`order-vs-coordinate` 2PC 与 Calvin 式预排序对比** — 同一批事务，
  一边在线协调、一边先定序后执行；比较往返次数、中止率与热点表现。

### 第5讲 分布式查询
- **`join-strategies` 本地 / 广播 / 重分区 Join** — 代价模型 + 字节流动画 + 交叉点曲线。（`dataflow`）
- **`pushdown-aggregation` 下推与两阶段聚合** — 四种变体比较扫描行数、网络字节、中间峰值。

### 第6讲 快照与恢复
- **`aries-recovery` Analysis → Redo → Undo** — WAL 条带、脏页表、`pageLSN` 判定、CLR；
  参数：检查点位置、崩溃 LSN、刷盘策略；支持“恢复中再次崩溃”。（`storage`）
- **`snapshot-catchup` 快照安装与追赶** — 落后副本 → InstallSnapshot → 追日志 →
  才允许提供一致读；解释“进程已启动 ≠ 可以服务”。

### 第7讲 验证与可靠性
- **`jepsen-loop` 生成 / 注入 / 记录 / 检查** — 在上面的集群上跑生成器与故障调度，
  产出历史，交给线性一致检查器（复用 `examples/history_checker.py`）；
  自动**收缩**反例，输出最小历史与种子。配合“种子扫描”演示覆盖率与可复现性的区别。

### 第8讲 架构对比
- **`architecture-lenses` 同一笔跨分片事务的五条请求路径** —
  Spanner 式 commit-wait、CockroachDB 式 Range+Raft+HLC、Calvin 式预排序、
  FoundationDB 式 OCC、TiDB 式 Percolator 2PC。逐条画出：谁定序、谁必须响应、
  何时发现冲突、成功前还在等什么。
  **界面固定标注：这是教学化的路径模型，不是产品性能基准。**

合计 17 个场景，覆盖全部 10 讲。第9讲（大作业）不新增场景，改为提供
`lab/run.py export` 与仓库结构模板，直接对接“原始数据不修改”的交付要求。

## 7. 课堂交互设计

- **先猜后验**：每个场景声明 `prediction`（问题 + 2–4 个选项 + 正确答案 + 解释）。
  运行前界面先显示问题让学生举手表决，运行后揭晓。对应课件里的 `_class: question` 页。
- **教学要点自动暂停**：`note` 事件触发时可自动暂停（可关），方便讲解。
- **对比运行**：沿 `compare_axis` 双舞台同步播放 + 指标差异表。
- **种子扫描**：批量跑种子，列出反例种子；用来讲“固定种子不等于覆盖充分”。
- **投影模式**：一键放大字号、提高对比度、隐藏侧栏。
- **键盘**：`空格` 播放/暂停，`←/→` 单步，`R` 重跑，`C` 对比，`1–9` 切换演示。
- **导出**：舞台 SVG（可直接进课件）、轨迹 JSON、事件日志 Markdown（可粘进讲者备注）。

配色与字体沿用 `theme/distributed-db.css`（`--ink #17202a`、`--paper #fbfaf7`、
`--accent #0b6b6b`、`--accent-2 #c14f2f`），截图放进幻灯片不违和。

## 8. 故障注入（统一参数，场景按需声明支持项）

`partition(分组) / heal`、`drop(概率)`、`delay(区间)`、`reorder`、`duplicate`、
`crash(丢失易失状态)`、`restart`、`pause(进程暂停，区别于崩溃)`、
`clock_skew / clock_jump`、`slow_disk / lost_fsync`。

故障既可由参数按概率注入，也可在时间轴上**手动点选时刻触发**（课堂即兴演示用）。

## 9. 不变式与检查器

- `at_most_once`：同一 `request_id` 至多生效一次
- `acked_not_lost`：已向客户端确认成功的写不会消失
- `committed_prefix_stable`：已提交前缀不被未来推翻
- `bank_conservation`：账户总额守恒
- `linearizable`：复用并扩展 `examples/history_checker.py`（历史长度设上限并在界面注明）
- `snapshot_isolation` / `serializable`：基于 wr/ww/rw 依赖图找环（Elle 思路的教学简化版）

检查失败时输出**最小反例**（逐步删减操作直到不再违规）+ 复现命令行。

## 10. 目录结构与启动

```
lab/
  PLAN.md                    # 本文件
  README.md                  # 教师使用说明 + 每个演示的三分钟脚本
  run.py                     # CLI: list / run / compare / sweep / export / serve
  cluster_lab/
    kernel.py                # 逻辑时钟、事件堆、随机源、轨迹记录器
    network.py               # 分区、丢包、延迟、乱序、重复
    trace.py                 # 事件模型与 JSON 序列化
    params.py                # 参数模式（表单由它生成）
    registry.py              # 场景注册
    invariants.py            # 不变式与最小反例收缩
    checkers/                # linearizable.py / bank.py / isolation.py
    nodes/                   # raft.py / kv.py / txn.py / storage.py / routing.py / query.py
    scenarios/               # 每个演示一个文件，导入即注册
    server.py                # http.server + JSON API（仅 127.0.0.1）
  web/
    index.html  app.js  player.js  style.css
    views/  cluster.js timeline.js shards.js dataflow.js storage.js history.js
  tests/
    test_kernel.py test_scenarios.py test_checkers.py test_server.py
```

启动方式（三选一，都不需要安装任何东西）：

```bash
python3 lab/run.py serve            # 打开 http://127.0.0.1:8000
```

```bash
corepack pnpm lab                   # 与仓库其余命令风格一致
```

```bash
python3 lab/run.py run raft-replication --seed 2026 --commit-rule naive_majority
```

`package.json` 新增：`"lab"`、`"test:lab"`；`check` 是否纳入 `test:lab` 待定。

## 11. 与既有代码的关系

lab **复用**而不是复制 `examples/` 里的模块，让学生在动画里看到的就是课件代码：

- `examples/raft_rules.py` → 选举与提交规则判定
- `examples/sharding.py` → 稳定散列与倾斜工作负载
- `examples/history_checker.py` → 线性一致检查
- `examples/write_skew.py`、`recovery.py` → 对应场景的状态机骨架
- `examples/deterministic_network.py` → 事件排序规则的原型

`examples/` 保持独立可读（课件里仍然引用它们），lab 通过 `examples.*` 导入。

## 12. 测试

- **确定性**：同一 `(scenario, params, seed)` 两次运行产生逐字节相同的轨迹 JSON。
- **正向**：每个场景在声明的默认参数与 5 个预声明种子下，所有不变式成立。
- **反向（关键）**：故意设错的模式**必须**产生反例。例如
  `commit_rule=naive_majority` 必须出现已提交条目回退；`policy=any_node` 必须违反线性一致；
  `write_skew mode=si` 必须出现值班人数为 0。反向测试防止“演示其实什么都没证明”。
- **契约**：轨迹 JSON 通过 schema 校验；前端有一份最小回放冒烟测试（Node 或浏览器手测）。
- **接口**：`/api/*` 冒烟测试。

## 13. 分期交付

| 期 | 内容 | 验收 |
|---|---|---|
| 一 ✅ | 内核 + 轨迹 + 注册表 + 服务端 + 界面骨架 + `cluster` 视图 + `raft-election` / `raft-replication` / `ack-meaning` | 绪论与第2讲可直接开讲 |
| 二 | `history` 视图 + 检查器 + `cap-partition` / `read-path` / `two-phase-commit` / `snapshot-catchup` | 绪论主力演示与第4、6讲可用 |
| 三 | `timeline` + `shards` + `storage` 视图 + `four-clocks` / `nondeterministic-apply` / `shard-strategies` / `rebalance-routing` / `aries-recovery` | 第1、3、6讲可用 |
| 四 | `dataflow` 视图 + `join-strategies` / `pushdown-aggregation` / `order-vs-coordinate` / `jepsen-loop` / `architecture-lenses` | 第5、7、8讲可用 |
| 五 | 对比运行、种子扫描、投影模式、导出、`lab/README.md` 教师脚本、全部测试 | 全课程可用，一条命令复现 |

每一期结束时 lab 都是可运行、可演示的，不存在“半成品阶段”。

### 第一期交付记录（已完成）

- 内核、网络、轨迹、参数模式、注册表、不变式检查器、HTTP 服务端、命令行入口
- `cluster` 视图：节点环 + 消息小球 + 日志矩阵（含“被覆盖”冲突标记）
- 三个演示：`ack-meaning`、`raft-election`、`raft-replication`（含 Figure 8 编排）
- 22 项测试：契约、正向（5 个预声明种子）、反向（三种错误配置必须产生反例）
- 界面：先猜后验、要点自动暂停、单步与拖动、种子扫描、投影模式、SVG/JSON 导出

下一步进入第二期：`history` 视图与 `cap-partition` / `read-path` /
`two-phase-commit` / `snapshot-catchup`。

## 14. 非目标与风险

- **不是生产实现**：教学版 Raft 省略持久化细节与成员变更的完整处理，界面注明。
- **不是性能基准**：所有“延迟/吞吐”都是逻辑 tick，不可与真实系统比较。
- **架构对比是风格化模型**：只比较请求路径与等待关系，不代表任何产品的实测行为；
  相关页面固定标注并链接 `sources.md`。
- **线性一致检查是暴力搜索**：历史长度设上限（默认 ≤ 14 个并发操作），超限时界面提示改用不变式检查。
- **浏览器兼容**：目标为现代 Chrome/Edge/Safari；不做 IE 与离线打包（如需离线，`dist` 之外单独处理）。

## 15. 已确认的决定

1. 目录名使用 `lab/`。
2. **不改动 `slides/`**：lab 作为独立演示包交付，幻灯片保持原样。
3. `pnpm check` **不**纳入 `test:lab`；lab 自带 `python3 lab/run.py test` 与 `test:lab` 脚本。
4. 界面文案中文，关键术语在括号内附英文（如“任期 (term)”）；代码标识符与注释英文。
5. 网页外观沿用课件主题配色与字体，并做现代化排版。
