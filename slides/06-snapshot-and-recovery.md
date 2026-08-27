---
marp: true
theme: distributed-db
size: 16:9
paginate: true
math: katex
html: true
title: 第6讲：快照、恢复与一致读
author: Hong
footer: 分布式数据库开发
---

<!-- _class: lead -->

# 第6讲：快照与恢复

## 重建一个满足协议的状态

Hong

---

<!-- _class: question -->

## 系统重启后，哪些状态必须重新证明？

- 哪些事务已经提交？
- 哪些页面包含未落盘更新？
- 哪些写入需要重做？
- 哪些未完成事务需要撤销？
- 副本追上到哪个日志位置后才能服务读取？

恢复过程本身也是一段需要正确性的执行历史。

---

## RPO 与 RTO 描述不同目标

- **RPO**：可接受丢失到哪个时间点的数据
- **RTO**：从故障到恢复服务允许多长时间

增加日志持久化频率改善 RPO；检查点、并行恢复和分层加载改善 RTO。两者都依赖明确故障模型。

---

## WAL 的两个关键先后约束

1. 描述页面更新的日志必须先于脏页落盘
2. 报告事务提交前，提交记录必须达到承诺的持久化点

这让系统在数据页与日志写入不同步时，仍能从日志判断如何恢复。

---

## LSN 把日志位置与页面状态关联起来

- 每条日志记录有单调递增的 Log Sequence Number
- 页面保存 `pageLSN`，表示已反映的最新日志位置
- Redo 时，若页面已包含该更新，就跳过重复执行

幂等 Redo 依赖页面与日志之间这个可检查的关系。

---

## 检查点缩短恢复扫描，不用暂停世界

模糊检查点可以记录：

- 活跃事务表
- 脏页表及每页最早可能需要 Redo 的 LSN
- 检查点开始/结束位置

前台事务可继续运行；恢复时再结合后续日志重建准确状态。

---

<!-- _class: section -->

# ARIES：Analysis → Redo → Undo

## 先重复历史，再撤销失败者

---

## 三阶段各自回答一个问题

![ARIES 三阶段恢复](../assets/diagrams/07-aries-recovery.svg)

<!--
[Sources]
- Mohan et al., ARIES: A Transaction Recovery Method Supporting Fine-Granularity Locking and Partial Rollbacks Using Write-Ahead Logging — https://www.cs.cmu.edu/~15849g/readings/mohan92.pdf
-->

---

## Analysis 重建崩溃前的控制信息

从最近检查点开始扫描日志：

- 恢复事务表，区分 winner 与 loser
- 恢复脏页表及 `recLSN`
- 找出 Redo 的安全起点

Analysis 主要重建现场，不是立即修改业务页面。

---

## Redo 重复历史，而不只重做已提交事务

ARIES Redo 会重演必要的页面更新，包括 loser 事务此前的更新。

原因：先把数据库恢复到崩溃瞬间的页面状态，再统一执行逻辑撤销，推理更简单。

<!--
[Sources]
- Mohan et al., ARIES, repeating history paradigm — https://www.cs.cmu.edu/~15849g/readings/mohan92.pdf
-->

---

## Undo 逆序撤销 loser，并写 CLR

- 按事务日志链从后向前撤销
- 每次撤销写 Compensation Log Record
- CLR 描述已完成的补偿和下一步 Undo 位置
- 恢复期间再次崩溃时，不会无限重复同一撤销

---

## 一个页面是否需要 Redo，要经过多重检查

常见判断：

1. 页面在脏页表中
2. 记录 LSN 不早于该页 `recLSN`
3. 磁盘页面的 `pageLSN` 小于记录 LSN

只有都满足时才应用更新并推进 `pageLSN`。

---

## 分布式恢复还要恢复复制协议位置

本地存储恢复后，副本仍需确认：

- 当前任期与投票持久状态
- 日志末尾、提交索引和应用索引
- 快照包含到哪个索引与任期
- 是否需要从 Leader 安装快照或追日志

“进程已启动”不等于副本可以对外服务。

---

## 快照是某个逻辑位置的完整状态

一个可安装快照至少要绑定：

- 状态机内容
- 对应日志索引/事务时间戳
- 校验和与格式版本
- 去重表、元数据等协议状态

快照之后仍要连续重放增量日志。

---

<!-- _class: section -->

# 一致读选择的是版本与等待条件

## “读副本”本身不是一致性模型

---

## MVCC 快照读需要可见性规则

对读时间戳 `read_ts`：

- 选择提交时间不晚于 `read_ts` 的最新版本
- 忽略未提交、已回滚或对该事务不可见的版本
- 同一事务的多次读取是否固定 `read_ts`，取决于隔离级别

---

## 全局一致快照要求各分片使用同一逻辑边界

不能让：

- 分片 A 读取事务 T 之后的状态
- 分片 B 读取事务 T 之前的状态

协调者需要一个一致的读时间戳，并确保每个参与分片都能安全读取到该位置。

---

## 强一致 Follower Read 可以等待，而不是返回旧值

一种常见路径：

1. Follower 通过 ReadIndex 等机制确认 Leader 的提交位置
2. 等待本地应用到该位置
3. 再执行读取

TiDB 的 Follower Read 就使用这类思路，它不同于显式陈旧读。

<!--
[Sources]
- PingCAP, TiDB Follower Read — https://docs.pingcap.com/tidb/stable/follower-read/
- Ongaro and Ousterhout, In Search of an Understandable Consensus Algorithm — https://raft.github.io/raft.pdf
-->

---

## 有界陈旧读把新鲜度写进契约

例如：允许读取“最多落后 5 秒”的一致快照。

收益：

- 读可在更近副本执行
- 减少 Leader 压力
- 避免等待最新日志

前提：业务确实能容忍该陈旧边界，并在界面和 API 中可见。

---

## 会话语义解决用户连续操作的直觉

- **Read Your Writes**：后续读能看到本会话此前成功写入
- **Monotonic Reads**：连续读取的版本不会倒退
- **Writes Follow Reads**：写入建立在此前已观察版本之后

会话令牌可携带最低版本要求，让不同副本知道要追到哪里。

---

## 时间旅行查询依赖版本保留策略

```sql
SELECT *
FROM account_history
AS OF SYSTEM TIME :snapshot_time
WHERE account_id = :id;
```

能力边界取决于：

- MVCC 垃圾回收窗口
- 归档日志是否连续
- Schema 变化如何解释旧版本

---

## PITR = 基线快照 + 连续日志 + 明确目标点

Point-in-Time Recovery 流程：

1. 恢复一个早于目标时间的完整快照
2. 校验并按序重放归档日志
3. 在目标事务位置或时间戳停止
4. 以新身份开放恢复实例，避免覆盖原集群

---

## 固定崩溃点让恢复演示可重复

```python
CRASH_AFTER_LSN = 7

for record in workload:
    wal.append(record)
    if record.lsn == CRASH_AFTER_LSN:
        raise SimulatedCrash(record.lsn)
    buffer.apply(record)
```

崩溃点、刷盘策略和随机种子都应成为实验参数。

---

## 恢复测试不能只检查“进程已启动”

至少验证：

- winner 效果存在，loser 效果不存在
- 页面与索引结构一致
- 去重表与业务数据同步恢复
- 副本提交/应用位置单调
- 恢复后同一请求重试不会重复生效

---

## 常见反模式

- 只有备份，没有做恢复演练
- 快照成功但归档日志存在缺口
- 以进程存活代替数据一致性检查
- Follower 尚未追上就提供“最新读”
- 时间旅行窗口承诺长于实际版本保留时间

---

## 思考题

1. ARIES 为什么先 Redo loser 的历史，再 Undo？
2. `pageLSN` 如何避免重复 Redo？
3. 强一致 Follower Read 与陈旧读的差别是什么？
4. RPO 很小是否意味着 RTO 也一定很小？

---

<!-- _class: takeaway -->

## 恢复把持久日志重新解释成满足协议的状态

- WAL 与 LSN 建立可判断的落盘顺序
- ARIES 通过 Analysis、Redo、Undo 重建一致状态
- 快照必须绑定逻辑位置，并与后续日志连续
- 一致读由版本边界和等待条件定义

下一讲：如何从客户端历史证明这些承诺真的成立？
