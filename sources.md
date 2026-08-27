# 资料来源

本文件记录新版课件所依据的论文与官方文档。系统行为可能随版本变化；涉及产品能力时应优先重新核对相应官方文档。

## 基础理论

- Leslie Lamport, [Time, Clocks, and the Ordering of Events in a Distributed System](https://www.microsoft.com/en-us/research/publication/time-clocks-ordering-events-distributed-system/), 1978.
- Seth Gilbert and Nancy Lynch, [Brewer’s Conjecture and the Feasibility of Consistent, Available, Partition-Tolerant Web Services](https://www.cs.princeton.edu/courses/archive/spr22/cos418/papers/cap.pdf), 2002.
- Daniel J. Abadi, [Consistency Tradeoffs in Modern Distributed Database System Design](https://www.cs.umd.edu/~abadi/papers/abadi-pacelc.pdf), 2012.
- Diego Ongaro and John Ousterhout, [In Search of an Understandable Consensus Algorithm](https://raft.github.io/raft.pdf), 2014.
- Hal Berenson et al., [A Critique of ANSI SQL Isolation Levels](https://www.microsoft.com/en-us/research/publication/a-critique-of-ansi-sql-isolation-levels/), 1995.
- C. Mohan et al., [ARIES: A Transaction Recovery Method Supporting Fine-Granularity Locking and Partial Rollbacks Using Write-Ahead Logging](https://www.cs.cmu.edu/~15849g/readings/mohan92.pdf), 1992.
- Kyle Kingsbury and Peter Alvaro, [Elle: Inferring Isolation Anomalies from Experimental Observations](https://www.vldb.org/pvldb/vol14/p268-alvaro.pdf), 2020.
- Jepsen, [Consistency Models](https://jepsen.io/consistency/models).

## 系统论文与官方文档

- James C. Corbett et al., [Spanner: Google’s Globally-Distributed Database](https://www.usenix.org/conference/osdi12/technical-sessions/presentation/corbett), 2012.
- Google Cloud, [Spanner: TrueTime and external consistency](https://docs.cloud.google.com/spanner/docs/true-time-external-consistency).
- Alexander Thomson et al., [Calvin: Fast Distributed Transactions for Partitioned Database Systems](https://www.cs.yale.edu/homes/thomson/publications/calvin-sigmod12.pdf), 2012.
- FoundationDB, [Developer Guide](https://apple.github.io/foundationdb/developer-guide.html).
- FoundationDB, [Simulation and Testing](https://apple.github.io/foundationdb/testing.html).
- Cockroach Labs, [Developer Basics](https://www.cockroachlabs.com/docs/stable/developer-basics.html).
- PingCAP, [TiDB Transactions](https://docs.pingcap.com/tidb/stable/transaction-overview/).
- PingCAP, [TiDB Transaction Isolation Levels](https://docs.pingcap.com/tidb/stable/transaction-isolation-levels/).
- PingCAP, [TiDB Architecture FAQs](https://docs.pingcap.com/tidb/stable/tidb-faq/).
- PingCAP, [TiDB Follower Read](https://docs.pingcap.com/tidb/stable/follower-read/).

## 课件工具

- Marp, [Markdown Presentation Ecosystem](https://marp.app/).
- Marp CLI, [官方 npm 包](https://www.npmjs.com/package/@marp-team/marp-cli).
- Mermaid CLI, [官方 npm 包](https://www.npmjs.com/package/@mermaid-js/mermaid-cli).
