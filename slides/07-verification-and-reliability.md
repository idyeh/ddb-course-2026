---
marp: true
theme: distributed-db
size: 16:9
paginate: true
math: katex
html: true
title: 第7讲：验证与可靠性
author: Hong
footer: 分布式数据库开发
---

<!-- _class: lead -->

# 第7讲：验证与可靠性

## 系统日志说“没问题”，客户端历史可能不同意

Hong

---

## Safety 与 Liveness 回答不同问题

- **Safety**：坏事永不发生，例如两个已提交值互相矛盾
- **Liveness**：在规定条件下，好事终会发生，例如请求最终完成

超时、死锁和永久重试多属活性问题；丢写、重复扣款和读到不可能状态属于安全性问题。

---

## 不变式必须能从观测历史检查

好的验证目标：

- 请求编号唯一且最多生效一次
- 银行总额守恒
- 成功写入之后的强一致读不返回旧值
- 事务效果不会只显示一半

“结果看起来正常”无法自动化，也无法定位反例。

---

<!-- _class: section -->

# 一致性不是一条单轴刻度

## 先区分对象、事务、会话和真实时间

---

## Linearizability 约束单次操作与真实时间

每个操作像在调用与返回之间某个瞬间生效；若 A 已返回后 B 才开始，A 必须排在 B 之前。

适合讨论：

- 单键寄存器
- 队列操作
- 原子 Compare-And-Set

<!--
[Sources]
- Jepsen, Consistency Models: Linearizability — https://jepsen.io/consistency/models/linearizable
-->

---

## Sequential Consistency 保留程序顺序，不保证真实时间

所有操作仍需存在一个共同顺序，并保持每个进程自己的操作次序。

但一个已经完成的写，可以在该共同顺序中被放到后来开始的读之后；这在线性一致性中不允许。

---

## Causal Consistency 只强制因果相关事件的顺序

- 读到一条消息后发出的回复必须在原因之后可见
- 并发、互不影响的写不必在所有副本上采用同一顺序
- 常与会话保证组合，改善用户连续操作体验

它不是“最终一致再快一点”，而是明确保留 happened-before。

---

## 会话保证各自保护一个用户视角

- Read Your Writes
- Monotonic Reads
- Monotonic Writes
- Writes Follow Reads

这些保证之间并非自动互相推出；也不能单独替代多对象事务隔离。

---

## Serializability 约束事务等价于某个串行顺序

事务可以并发执行，但结果必须与逐个执行某个顺序等价。

它不要求这个串行顺序服从墙上时间；因此需要更强实时语义时，应讨论 **Strict Serializability**。

---

## Strict Serializability 连接事务与真实时间

它同时要求：

- 多对象事务可串行化
- 不重叠事务的串行顺序服从真实时间先后

Spanner 把这一性质称为 external consistency；FoundationDB 官方也声明普通事务默认提供 strict serializability。

<!--
[Sources]
- Google Cloud, Spanner TrueTime and external consistency — https://docs.cloud.google.com/spanner/docs/true-time-external-consistency
- Apple, FoundationDB Developer Guide — https://apple.github.io/foundationdb/developer-guide.html
-->

---

## SI 与 RC 应按异常集合解释

- **Snapshot Isolation**：一致快照 + 写写冲突规则；可能写偏差
- **Read Committed**：每次读取只看已提交值；同一事务内两次读可能不同
- 产品的兼容名称可能与理论模型不完全一致

TiDB 的 `REPEATABLE READ` 官方解释为 Snapshot Isolation，并另支持 Read Committed。

<!--
[Sources]
- Berenson et al., A Critique of ANSI SQL Isolation Levels — https://www.microsoft.com/en-us/research/publication/a-critique-of-ansi-sql-isolation-levels/
- PingCAP, TiDB Transaction Isolation Levels — https://docs.pingcap.com/tidb/stable/transaction-isolation-levels/
-->

---

<!-- _class: section -->

# Jepsen 从客户端外部构造证据

## 生成操作、注入故障、记录历史、检查模型

---

## 单个验证循环由五个部件组成

![Jepsen 风格验证循环](../assets/diagrams/08-jepsen-loop.svg)

---

## Generator 要产生能暴露错误的操作

设计原则：

- 每次写入使用唯一值，便于追踪来源
- 混合读、写、CAS 或多键事务
- 控制并发度和热点比例
- 在恢复阶段继续观测，避免故障停止后立即结束

---

## Nemesis 破坏假设，但不能破坏测试本身

常见故障：

- 网络分区、丢包或非对称延迟
- 暂停、杀死与重启进程
- 磁盘空间或 I/O 故障
- 时钟偏差与跳变

测试控制面、客户端日志和时间记录也要可靠，否则会制造假阳性。

---

## History 必须保留不确定操作

一次操作至少记录：

- 进程/会话编号
- 唯一操作编号
- 调用时间、参数
- 返回时间、结果
- `ok / fail / info(unknown)` 状态

超时事务不能随意标成失败；它可能已经提交。

---

## 线性化检查器寻找合法的瞬时生效顺序

对寄存器历史，检查器尝试排列并发操作，同时满足：

- 对象的顺序规范
- 每个操作发生在调用与返回之间
- 不重叠操作服从真实时间

找不到合法顺序时，给出尽量小的反例历史。

---

## Elle 从可追踪值推断事务依赖图

Elle 使用可恢复、可追踪的对象操作，推断：

- write-read 依赖
- write-write 依赖
- read-write 反依赖

再检查与声明隔离级别冲突的结构；它不是对任意 SQL 历史的万能解析器。

<!--
[Sources]
- Kingsbury and Alvaro, Elle: Inferring Isolation Anomalies from Experimental Observations — https://www.vldb.org/pvldb/vol14/p268-alvaro.pdf
-->

---

## Append-only list 让版本来源更容易恢复

事务写入唯一元素：

```text
T1: append(x, 101)
T2: read(x) -> [101]; append(x, 202)
T3: read(x) -> [101, 202]
```

完整列表暴露版本轨迹，比只返回最后一个标量值包含更多依赖信息。

---

## 银行工作负载检查两类性质

业务不变式：

- 所有账户余额总和不变
- 单个余额可按业务规则限制非负

历史性质：

- 事务原子可见
- 已确认转账不会消失
- 重试不会重复转账

仅检查最终总额，可能漏掉中间读异常。

---

## 固定种子保存一条可复现反例

```python
SEED = 2026
rng = Random(SEED)

for step in range(200):
    operation = generate_operation(rng)
    maybe_inject_fault(rng, step)
    record(run(operation))
```

发现失败后输出种子和缩减后的历史；回归测试固定该种子。

---

## 多种种子与系统化枚举承担不同职责

- 固定失败种子：稳定复现与调试
- 随机多种子：扩大调度覆盖
- 小模型枚举：穷举有限状态空间
- 形式化规范：证明模型内安全性质

可靠性证据应说明采用了哪一种，而不是统称压力测试。

---

## FoundationDB 把确定性仿真用于生产开发

- 单线程模拟整个集群及故障
- 随机行为由种子驱动，失败可重放
- 同时建模网络、进程、存储与数据中心故障
- 与真实硬件测试和性能测试互补

<!--
[Sources]
- FoundationDB, Simulation and Testing — https://apple.github.io/foundationdb/testing.html
- FoundationDB, Client Testing — https://apple.github.io/foundationdb/client-testing.html
-->

---

## 可信报告要把证据链写完整

1. 被测版本与配置
2. 声明的一致性/隔离模型
3. 工作负载与生成器参数
4. 故障时间线
5. 检查器与假设
6. 最小反例、种子与复现步骤

---

## 常见错误结论

- “运行一小时没出错，所以线性一致”
- “最终余额正确，所以事务一定原子”
- “超时就是失败，可以直接重试新请求”
- “检查器报环，所以数据库一定有 Bug”
- “固定种子可复现，所以覆盖充分”

每个结论都要回到检查器假设和历史证据。

---

## 思考题

1. Linearizability 与 Serializability 分别约束什么？
2. 为什么超时操作必须保留为 unknown？
3. Elle 为什么偏好带完整版本轨迹的数据类型？
4. 固定种子与形式化证明的边界分别是什么？

---

<!-- _class: takeaway -->

## 正确性不是运行结果，而是历史满足模型

- 先选择正确的一致性或隔离模型
- 生成可追踪操作，保留不确定结果
- 故障注入与检查器共同构成证据
- 固定种子便于重放，多种策略扩大覆盖

下一讲：用统一维度比较真实分布式数据库架构。
