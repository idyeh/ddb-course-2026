# 课堂集群实验室 (cluster-lab)

一个贯穿全课程的分布式系统演示工具。**只用 Python 标准库和浏览器**，不需要 pip、
不需要 Node，也不参与课件构建。

```bash
python3 lab/run.py serve
```

浏览器会自动打开 <http://127.0.0.1:8000/>。左侧选演示，右侧改参数，中间看动画。

## 它是怎么工作的

一句话：**Python 是唯一的真相来源，网页只负责回放。**

```
python3 lab/run.py …  ──►  确定性内核 ──►  一条完整的事件轨迹 (trace JSON)
                                              │
                    浏览器 ◄─────────────────┘  只做回放：不含任何协议逻辑
```

因此课堂动画、命令行输出、单元测试和实验报告看到的是同一份证据；
`(演示, 参数, 种子)` 一旦相同，轨迹逐字节相同，动画也逐帧相同。

内核使用整数逻辑时钟与最小堆，全程不读墙上时间、不调用 `sleep`；
所有随机决定来自同一个 `Random(seed)`。播放速度只是回放倍率，不影响结果。

## 界面

| 区域 | 作用 |
|---|---|
| 左栏 | 演示菜单，按讲次分组 |
| 舞台 | 节点环 + 飞行中的消息 + 日志矩阵（行=节点，列=日志索引） |
| 时间轴 | 播放 / 暂停 / 单步 / 拖动 / 调速 |
| 教学要点 | 剧本在关键时刻抛出的讲解卡片 |
| 事件日志 | 逐条事件；勾选“全部消息”可看每一次收发 |
| 右栏 | 先猜后验、参数表单、不变式结果、指标 |

快捷键：`空格` 播放/暂停，`←`/`→` 上一/下一事件，`R` 重跑，`P` 投影模式，`1–9` 切换演示。

- **要点暂停**：默认打开。播放到教学要点时自动停下，方便讲解。
- **投影模式**：隐藏侧栏、放大字号，适合大屏。
- **导出图形 / 导出轨迹**：SVG 可直接放进幻灯片；JSON 是可引用的原始数据。

## 三分钟课堂脚本

### 绪论 · 确认是何意味 (`ack-meaning`)

1. **先猜后验**：把右上角的问题读给学生，让他们举手选。
2. 默认参数（多数派确认 + 复用 request_id）跑一遍：全部不变式通过。
3. 把「确认点」切到 **Leader 本地落盘即回复**，再跑：
   `已确认写不丢失` 变红——客户端手里那个 ok 指向一条被截断的日志。
4. 把「重试策略」切到 **每次重试生成新的 request_id**：
   `同一操作至多生效一次` 变红，计数器从 3 跳到 4。
5. 收尾：ok 的含义由确认点定义；超时只能推出 unknown；重试必须复用请求编号。

### 第2讲 · Raft 选举 (`raft-election`)

1. 先猜后验：隔离当前 Leader 之后，最多有几个自称 Leader 的节点？
2. 播放。第一幕看随机超时如何打破对称，第二幕看被隔离的 Leader 继续自称 Leader。
3. 指出 `每个任期至多一个 Leader` 仍然通过——安全性约束的是任期，不是时刻。
4. 换几个种子，看选举轨迹如何改变而结论不变。

### 第2讲 · 日志复制与提交规则 (`raft-replication`)

1. 保持 `figure8` 模式，先用 **Raft 规则** 跑完，注意 (e) 幕的教学要点。
2. 只把「提交规则」换成 **错误规则**，其他一律不动，再跑一遍。
3. 对照日志矩阵：两次运行的**最终日志完全相同**，唯一的差别是那个提交标记。
4. 错误规则下，索引 2 的条目被划掉并标注“被覆盖”，
   `已提交前缀不被推翻` 给出反例（点“跳到反例”可直接跳到那一刻）。
5. 收尾：复制得多 ≠ 已经提交。

> `figure8` 为受控编排：关闭自发选举超时、由剧本触发换届，用于稳定复现论文场景。
> 想看自然调度，切到「常规复制」模式。

## 命令行

```bash
python3 lab/run.py list
```

```bash
python3 lab/run.py run raft-replication --set commit_rule=naive_majority
```

```bash
python3 lab/run.py compare ack-meaning --axis ack --values majority local
```

```bash
python3 lab/run.py sweep ack-meaning --set ack=local --first 2026 --count 20
```

```bash
python3 lab/run.py export raft-election --seed 2026 --out results/raw/election-2026.json
```

`run` 在出现不变式反例时返回退出码 1，可以直接用在回归脚本里。
`sweep` 用来演示“固定种子提供重放，不提供覆盖率”：把反例种子记下来，加进回归测试。

## 测试

```bash
python3 lab/run.py test
```

```bash
python3 -m unittest discover -s lab -t lab -p 'test_*.py'
```

测试分三类：

- **契约**：轨迹格式良好，事件时间单调，每条消息恰好送达或丢弃一次。
- **正向**：正确配置在多个预先声明的种子下保持全部不变式。
- **反向**：故意设错的配置**必须**给出反例。一个永远不会失败的演示，
  什么也证明不了——所以 `ack=local`、`retry=new_id`、`commit_rule=naive_majority`
  都被要求产生对应的违反。

## 目录

```text
lab/
  run.py               命令行入口：list / run / compare / sweep / export / serve / test
  cluster_lab/
    kernel.py          确定性事件内核（逻辑时钟、最小堆、单一随机源）
    network.py         延迟、丢包、乱序、重复、分区、定点截留
    trace.py           事件模型与 JSON 序列化（与网页的唯一契约）
    params.py          参数模式，表单由它生成
    registry.py        场景注册表
    invariants.py      不变式检查器，全部基于轨迹而非模拟器内部状态
    nodes/             raft.py（教学子集）、kv.py（状态机与去重表）、client.py
    scenarios/         每个演示一个文件，导入即注册
    server.py          http.server + JSON API，仅绑定 127.0.0.1
  web/                 index.html / style.css / app.js / player.js / views/
  tests/               契约、正向、反向测试
  PLAN.md              全课程演示的实施计划与分期
```

## 与课件的关系

lab 复用课件里出现过的模块，学生在动画里看到的就是幻灯片上的那段代码：

- `examples/raft_rules.py` — 投票的日志新旧比较、当前任期提交条件
- `examples/sharding.py` — 稳定散列与倾斜工作负载（后续分片演示使用）
- `examples/history_checker.py` — 线性一致检查（后续验证演示使用）

lab 是独立包：不改动 `slides/`，也不参与 `pnpm build` 与 `pnpm check`。

## 边界

- **不是生产实现**。教学版 Raft 省略了持久化细节、成员变更与快照。
- **不是性能基准**。所有时间都是逻辑 tick，不可与真实系统比较。
- **多数派 = 存活节点也要能通信**。崩溃节点在恢复前不计入任何多数派。
