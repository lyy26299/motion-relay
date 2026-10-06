# ADR 0005 — 分层 freshness 与独立播放状态

Status: Accepted incrementally. Date: 2026-09-23.

## Context

一个决定可以计算正确，但在 await 之后已经过期；provider done 与用户听完也不是同一事件。基线在 receipt 后过滤不足以撤销已发送副作用。

## Decision

保留 stream_epoch/frame_id、session scope、turn generation、memory_epoch、evidence expiry。Bridge 新增 output generation 和 FeedbackLease；发送前、取消旧请求后、注入网络阶段与音频 delta 处再次检查相关 guard。过滤外来 response.done/audio.done；只记录 generation_completed，不冒充 played_at。

## Alternatives

一个 cancelled boolean 无法表达归属和继任；全局 EventEnvelope 一次性迁移成本较高；仅 TTL 不足以抵挡不同 session/turn 的迟到结果。

## Consequences

这是组合 contract，不是所有层都共用一个新类型。暂停和代次失效不保证已发出的网络消息被撤回。原生对话旁路、迟到 response.created 以及浏览器 playout ACK 仍未闭环。数据库 epoch 检查与网络发送也不是一个分布式事务。

## Migration

不能跨进程直接相减单调时钟。真实播放状态必须由带 response ID / generation 的边缘回执确认，未来另加 acknowledged 状态；不要把旧 completed 一概迁移为已播放。

## Verification

旧 LLM/tool 结果、memory epoch 改变、注入取消、迟到 done/delta、VAD 先失效、旧 lease 不结束新 lease。研究依据 R1/R7/R8/R14。
