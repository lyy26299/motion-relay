# ADR 0003 — 单槽反馈准入，拒绝过时的语音 backlog

Status: Accepted for guarded bridge only. Date: 2026-09-23.

## Context

基线 arbiter 文件为空；不同输入源竞争同一语音输出。排队播放所有动作提示会使“正确但迟到”变成错误反馈。

## Decision

单活动 lease、零 pending speech 队列、去重历史128。优先级：emergency100 > safety90 > answer80 > correction70 > instruction60 > rep50 > encouragement20 > summary10 > silence0。用户打断是撤销控制，不是需要排队的一句话。

直接问题高于常规纠正/报数，避免系统淹没用户；安全与紧急优先。此排序是当前工程策略，不是用户实验结论。cooldown 从准入时计算：rep0.8s、correction3s、encouragement8s、summary20s。

## Alternatives

FIFO 会积压旧提示；多优先级队列仍需过期回收和时长规划；完整语音调度器需要播放 ACK，目前没有足够契约支撑。

## Consequences

低优先级被拒绝，而非延后必播。exact lease matching 防止旧完成清除新状态。尚无 speech-duration-aware 调度；TTL 可能截断长话；native Qwen 自动对话未受统一仲裁。这些是限制，不是已完成功能。

## Migration

Bridge 的受控注入必须先取得 lease，再通过 freshness guard 发送；其他 provider 接入前先声明旁路。新反馈类别必须同时定义优先级、TTL、cooldown 和测试。

## Verification

安全抢占、重复、cooldown、有界历史、busy、silence、过期、旧完成、interruption 均有测试；另有 pose→mock voice 全链路测试。研究依据 R2/R7/R8。
