# ADR 0006 — 响应身份、原生准入与不确定时隔离

Status: Accepted for the V2.1 increment. Date: 2026-09-23.

## Context

重复/迟到 response.created 会改写当前响应；缺失 ID 的事件隐式指向 current；Provider 生成状态不是播放状态。原生自动回答与手动注入同时存在时，服务端 response ID 不能独自证明客户端请求因果关系。

## Decision

使用 SDK 无关的 ResponseWindow：一个活动响应、256项退役窗口、严格 ID 门。应用将原生响应准入接到原有 FeedbackArbiter；注入仅一个 pending 槽。未绑定请求被打断或发送/取消结果不确定时，输出保持阻断，显式连接/会话恢复后再接纳。生成失败、未完成、未知与真正完成分别记录。

## Alternatives

无限保存全部 ID 会导致长期内存增长；把下一个 response.created 无条件当作新请求会混淆因果；单个 cancelled 布尔值没有身份；未经 live 验证就改 VAD/metadata 可能破坏已有协议；替换整个媒体框架超过已复现缺陷的需要。

## Consequences

有限窗口不是终身反重放保护。保守隔离可能中止语音，降低可用性。原生输出获准不等于通过证据验证。legacy ordered injection candidate 仍无因果证明，feedback facts/receipt 标注 response_correlation=unverified。0.2秒是 cancel 等待预算，不是实际停止播放保证。无新依赖、无 schema 迁移，无真正浏览器播放 ACK。

## Migration

Provider 事件必须带显式 ID；成功 fixture 需声明 completed。SessionController 明确连接 native_response_gate。操作者使用新会话恢复，不修改私有状态绕过阻断。未修改模块继续遵循V2.0契约，详见 VOICE_OUTPUT_V2_1.md。

## Verification

纯状态测试、原生准入测试、固定SDK离线事件竞争、单槽覆盖/发送失败/关闭及旧reader测试。微基准测量身份门的本地开销而非端到端语音性能；最终实际测试状态见 TEST_REPORT.md。
