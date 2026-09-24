# ADR 0004 — 分离工作快照、历史能力和持久化完整性

Status: Accepted, storage release gate open. Date: 2026-09-23.

## Context

frozen event 容器仍持有可变 facts；writer 溢出后可误报 saved；Agent 需要历史事实但不应依赖数据库 schema。

## Decision

WorkingMemory 写入时冻结 JSON facts；view 不暴露可写权威事实。Runtime 经有界 MemoryWriter 交付事件；关闭先封锁 admission，再 drain。溢出/关闭超时变成 sticky integrity failure。保留 MCPMemoryDispatcher 的业务语义和 runtime-bound user scope。

## Alternatives

每次 view 深拷贝导致显著额外 CPU；同步数据库写入破坏实时路径；引入远端数据库或向量库并不解决此次已复现竞争。

## Consequences

冻结增加 ingest 开销，实测 MotionRuntime P95 为38.432µs，基线18.375µs；不宣称提速。accepted 只代表内存队列接受，不代表掉电可恢复。满队列仍可能丢事实，但不能再称完整保存。

SQLite 运行库版本/供应商补丁须在生产前核验 WAL-reset 修复；Python 包锁不能证明 SQLite 引擎已修复。本轮未改数据库 schema、未升级引擎。

## Migration

需要可写事件副本时显式转换，不修改 view。调用者区分 saved、closed、failed 和 interrupted；失败会话不得当作完整训练总结。新增后端应实现业务查询和 evidence envelope，而非暴露 SQL。

## Verification

快照 mutation、writer admission/close race、sticky overflow，以及原幂等/事务/删除隔离/检索/MCP contract 测试。研究依据 R10/R11/R12。
