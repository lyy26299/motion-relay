# ADR 0002 — 有界工作所有权，而不是无界等待取消

Status: Accepted. Date: 2026-09-23.

## Context

SQLite 检索/receipt 写入和不合作 coroutine 可能占住应用 loop 或延长 turn。取消 thread caller 后释放容量会造成“账面无任务、实际线程仍工作”。

## Decision

新增 OperationPool：同步 callback 在线程运行，异步 callback 保持 asyncio；未完成操作一直占 slot，完成后回收异常。AgentLoop 用 deadline/cancel/checkpoint 仲裁结果，不无界等待取消确认。Bridge IO 上限4，应用任务4加latest槽1；快路径只生成事实并进行不等待的交接。

## Alternatives

单用 wait_for 无法保证取消完成时间；无限 to_thread/create_task 无资源上界；重写为多进程 Actor 暂无成本收益证据。

## Consequences

turn 失效不代表 provider 已停止计费或线程已退出。操作池是每 owner 的上界，不是进程级限流；default executor 关闭仍可能等待。安全本地暂停不等待 SQLite，语音仍可能依赖网络。

## Migration

保持 AgentLoop 原公开 contract；接入有副作用 actor 时必须在副作用前检查 ActionContext.is_current。新 SDK 必须声明原生 timeout/cancel 能力。

## Verification

不合作异步 decider、调用者取消后同步工作占位、close 超时返回、慢存储不延误本地暂停、迟到 DB 结果不注入等测试通过。研究依据见 R1/R2/R5。
