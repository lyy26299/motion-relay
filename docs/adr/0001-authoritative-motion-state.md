# ADR 0001 — 权威动作事实仍由 MotionRuntime 所有

Status: Accepted for this increment. Date: 2026-09-23.

## Context

基线显式 pause 可被后续站立帧解除；新 stream epoch 会保留 partial rep。二者都能用无摄像头 fixture 复现，不是单纯命名问题。

## Decision

保留 MotionRuntime → SquatFSM → RepRecord 的唯一业务路径。在 Runtime 增加 application pause latch，只由 resume 清除；新 epoch 丢弃半次动作；旧 epoch/frame 不得推进状态。WorkingMemory 是只读快照出口，不是另一套计数器。LLM 不能创建 rep。

## Alternatives

只依赖 FSM 的 paused phase 无法区分视觉恢复和用户意图；由 Agent 推断暂停/计数会引入概率与延迟；完整 HFSM 引擎超过本轮最小修复需要。

## Consequences

显式暂停后的 standing 不再恢复计数，属于有意行为修正。视觉短暂丢失仍走原站立恢复规则。Python 私有字段约定不是恶意代码隔离。仅有 SquatFSM，其他 exercise 插件尚未实现。

## Migration

控制层使用 Runtime.pause/resume，不直接写 completed_reps。改变 exercise 应建立新的能力绑定与状态生命周期，不仅替换 UI 文本。

## Verification

`tests/test_runtime_boundaries.py`：持续来帧暂停、跨流 partial rep、旧 epoch、零时间戳。原8项针对基线重放得到7项失败，修改后通过；既有 rep/invalid-rep 测试保留。
