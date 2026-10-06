# Agent 架构研究：来源、解释与本项目决定

检索日期：2026-09-23。以下为针对当前代码缺陷的工程研究，不是穷尽性文献综述。论文条目主要核对摘要与版本信息，未声称完成全文实验复现。官方网页若无固定发布日期，以访问日期标记；特定历史规范不冒充最新规范。

## 1. 问题导向

本项目需要的是持续接收姿态事实、及时停止过期工作，并在有限语音通道上表达事实。需要分别回答：本地动作是否可信、Agent 是否及时返回、输出是否仍相关、语音是否真正被播放。通用语言模型质量不能代替后三个运行时约束。

研究结论按 **Fact → Interpretation → Design decision** 区分。Fact 是来源支持的内容；Interpretation 是本项目推论；Decision 是当前提交采用或拒绝的做法，不是论文原结论。

## 2. 来源登记

### R1. Python asyncio：取消是协作机制

来源：[Python 3.13 tasks 文档](https://docs.python.org/3.13/library/asyncio-task.html)，访问 2026-09-23。

**Fact**：任务取消是请求；`wait_for` 会等待被取消任务退出，可能超过给定 timeout；`wait` 的超时不会自动终止 pending 工作。

**Interpretation**：只写 `wait_for(tool, timeout)` 不足以保证本项目决策按预算结束。尤其同步工作放入线程后，调用者取消不等于线程停止。

**Decision**：新增有界 OperationPool，保留 pending 工作的所有权、容量占用和异常回收；turn 通过 cancellation/deadline 失效结果，不无界等待不合作工作。

**不适用与代价**：不宣称强杀线程或硬实时；需要原生超时、进程隔离和服务级资源治理来完成更强的故障隔离。

### R2. Reactive Streams：背压而非无界积压

来源：[Reactive Streams](https://www.reactive-streams.org/)，访问 2026-09-23。

**Fact**：规范讨论异步流中的非阻塞背压，避免接收方被无限缓冲压垮。

**Interpretation**：视频帧、待说话提示和账本事实有不同损失语义，不能统一使用一个无限 FIFO。

**Decision**：保留姿态 latest-value；新增 bridge 容量限制；反馈采用单槽、零 backlog；账本溢出必须显式标为完整性失败。

**不适用与代价**：不引入 JVM/Reactive Streams 实现，也不把丢弃帧等同于允许静默丢失 rep。可接受的 drop policy 由领域语义决定。

### R3. Talker–Reasoner 与 Fast/Slow

来源：[Agents Thinking Fast and Slow: A Talker-Reasoner Architecture](https://arxiv.org/abs/2410.08328)，2024-10-10，Christakopoulou、Mourad、Matarić。

**Fact**：文章把快速对话合成与较慢的多步推理/规划区分，并以睡眠教练为讨论场景。

**Interpretation**：交互路径和推理路径值得分离，但本文中的 Talker 仍是模型，不是本项目的确定性动作安全路径。

**Decision**：采用两个时间尺度的职责划分，不新增两个 LLM。MotionRuntime 负责事实；已有实时模型负责普通对话；受限 AgentLoop 负责业务工具与输出决策。

**不适用与代价**：不能用该论文证明姿态检测准确率、运动效果或本项目毫秒延迟。当前 bridge 的 decider 实际是 Python 策略，不应命名为已实现的独立深度推理模型。

### R4. ReAct：行动闭环不等于无限推理

来源：[ReAct](https://arxiv.org/abs/2210.03629)，2022-10-06 首发，2023-03-10 v3。

**Fact**：研究将推理和外部行动交错，用工具取得额外信息。

**Interpretation**：业务查询与证据返回应在可解释的循环中运行，但论文结果不能替本项目提供时限与取消保证。

**Decision**：保留现有 bounded tool loop、工具 allowlist 和 evidence-carrying decision，不改成无限 agent executor。

**不适用与代价**：不移植论文 benchmark 分数，不把测试里的 deterministic decider 当作通用推理质量验证。

### R5. Actor 模型

来源：[Akka typed actors 官方介绍](https://doc.akka.io/libraries/akka-core/current/typed/actors.html)，访问 2026-09-23。

**Fact**：Actor 使用消息交换并封装自身状态。

**Interpretation**：单一状态所有者、显式消息交接与本项目并发问题相关；采用这个原则不要求采用 Actor 框架。

**Decision**：维持一个应用 loop 和专职 worker，通过 bounded callback/queue 交接。

**不适用与代价**：完整 Actor 重写增加 mailbox、supervision、序列化与迁移测试工作，目前没有跨进程扩展证据支持这笔成本。

### R6. 状态图与 HFSM

来源：[W3C SCXML](https://www.w3.org/TR/scxml/)，访问 2026-09-23。

**Fact**：SCXML 表达状态图、层次化状态与事件驱动转换。

**Interpretation**：应用暂停和动作站立恢复属于不同层次；混用一个布尔值会丢失控制意图。

**Decision**：在 MotionRuntime 增加应用暂停锁存，保留 SquatFSM 的视觉恢复，不引入 XML 状态机引擎。

**不适用与代价**：这不是已实现完整“预检→校准→训练→休息→总结”HFSM；全局训练流程仍需独立设计和测试。

### R7. LiveKit：语音生成与播放生命周期

来源：[Agent speech and audio](https://docs.livekit.io/agents/multimodality/audio/)，访问 2026-09-23。

**Fact**：官方音频接口区分 speech handle、等待播放以及中断。

**Interpretation**：输出需要独立状态，而不是收到模型结束事件就标记用户已听完。

**Decision**：Qwen 回调记录 generation_completed，不填写未经确认的 played_at；文档保留浏览器真实播放 ACK 缺口。

**不适用与代价**：不把 LiveKit API 直接套入当前 Vision-Agents/Qwen 适配器，也不声称本项目已具备 SpeechHandle 的全部语义。

### R8. Qwen Realtime 事件

来源：[Alibaba Cloud Realtime server events](https://www.alibabacloud.com/help/en/model-studio/server-events)，访问 2026-09-23。

**Fact**：服务端响应事件携带 response ID；response.done 表示响应生成阶段结束，而非本地扬声器确认。

**Interpretation**：旧响应的 done/delta 必须按归属过滤，不能无条件作用于当前 feedback。

**Decision**：补充 ID 过滤、注入有效性回调及相关离线 SDK 测试。

**不适用与代价**：官方页面可能描述新能力；本项目仍固定 vision-agents 0.6.9，未进行云 API 升级/真实可用性验证。取消后迟到的 response.created 仍需更强请求关联。

### R9. WebRTC、AEC 与浏览器捕获

来源：[W3C Media Capture and Streams](https://www.w3.org/TR/mediacapture-streams/)，访问 2026-09-23。

**Fact**：媒体捕获使用 constraints/settings 表达音频特征，包括回声消除设置。

**Interpretation**：请求 echoCancellation 与实际环境中实现有效 AEC 不是同一份证据。

**Decision**：保留浏览器 AEC 和本地 WebRTC 传输，测试协议与音频队列；将扬声器回声、真实打断延迟单列硬件验收。

**不适用与代价**：不以 loopback 测试宣称真实房间 AEC 达标；云端仍是 WebSocket，不把浏览器边缘 WebRTC 混称云端 WebRTC。

### R10. SQLite WAL：吞吐、检查点、持久化与上游修复

来源：[SQLite WAL 官方文档](https://www.sqlite.org/wal.html)，页面更新 2026-04-13，访问 2026-09-23。

**Fact**：WAL 允许读写并发但仍只有一个 writer；checkpoint 有额外 IO；NORMAL 与 FULL 的断电持久性不同。官方报告了稀有 WAL-reset 竞争，3.51.3 起修复，另列 3.44.6/3.50.7 回移修复。

**Interpretation**：后台线程只把等待移离实时 loop，不会自动消除丢失风险。CI 的 SQLite 3.45.1 版本号在上游受影响范围，但发行商补丁状态尚未核实。

**Decision**：保留本地账本，修复溢出后误报成功；生产前核验运行库修复及备份/恢复。不宣称已升级 SQLite 引擎或已经复现损坏。

**不适用与代价**：不把 SQLite 改成远端数据库；不通过放宽测试掩盖持久性问题。

### R11. MCP 工具契约

来源：[MCP tools specification 2025-06-18](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)，固定历史规范，访问 2026-09-23。

**Fact**：工具具有名称、输入 schema、结构化输出及错误语义；协议强调输入验证与访问控制。

**Interpretation**：协议传输不等于授权。安全 scope 应来自 runtime，不接受模型选择用户。

**Decision**：保留 memory/knowledge/profile 的业务 API；不暴露任意 SQL；延续严格参数、用户隔离、证据 envelope 测试。

**不适用与代价**：不声称此规范为最新，也不要求桌面 bridge 绕经远端 MCP 网络才能调用本地能力。

### R12. MemGPT 与记忆层次

来源：[MemGPT](https://arxiv.org/abs/2310.08560)，2023-10-12 首发，2024-02-12 v2。

**Fact**：论文借鉴分层存储管理有限模型上下文与更大的外部记忆。

**Interpretation**：对话上下文、长期个人信息和权威动作账本应分层，但动作事实不适合交给 LLM 自由改写。

**Decision**：WorkingMemory 继续 bounded；长期 numeric fact 从 ledger 查询；consolidation 保持可追溯。

**不适用与代价**：没有新增向量库、LLM 记忆操作系统或自动抽象训练偏好；不能把现有 FTS/词法检索宣传为已实现 embedding RAG。

### R13. 工具与最终结果评测

来源：[τ-bench](https://arxiv.org/abs/2406.12045)，2024-06 首发；[ALCE](https://arxiv.org/abs/2305.14627)，2023-05 首发；访问 2026-09-23。

**Fact**：τ-bench 关注工具、用户与规则交互；ALCE 区分有引用回答的引用质量等维度。

**Interpretation**：tool selection、arguments、execution、scope、evidence attribution、最终 claim 应分别评分。证据 ID 存在不代表自然语言必然正确。

**Decision**：本轮先用 deterministic tests 验证 contract 和实际副作用；后续另建带人工 claim 标注的冻结数据集。

**不适用与代价**：没有实际开展模型对比或用户实验，也没有用测试通过率替代任务成功率、引用支持率或运动效果。

### R14. Observability

来源：[OpenTelemetry traces](https://opentelemetry.io/docs/concepts/signals/traces/)，访问 2026-09-23。

**Fact**：trace/span 提供跨操作关联和时间信息。

**Interpretation**：仅有“总体耗时”不能解释慢在推理、DB、TTS 还是播放。

**Decision**：定义分段观测点与 correlation 规划；新增边界日志、拒绝计数和可重复 microbenchmark。

**不适用与代价**：没有宣称本轮完成 OTel 全链路接入。跨浏览器/服务端单调时钟需要映射，不能直接相减。

## 3. 三个候选方案

| 方案 | 复杂度与迁移 | 延迟与故障隔离 | 可测试性、维护与扩展 | 当前判断 |
|---|---|---|---|---|
| A：保留核心，增量建立 fast/slow 边界 | 低至中；保存 API、数据库、测试 | IO 离开 loop；有界工作；仍是同进程隔离 | 小补丁可回滚，当前缺陷可直接复现；可继续加入新 exercise/provider | **采用** |
| B：完整事件总线 + 独立 voice scheduler + 统一 event envelope | 中至高；需所有 provider/事件适配 | 能统一出口，但错误迁移也会扩大故障面 | 长期可观测性更清晰；需要大量跨层集成测试 | 下一阶段按真实瓶颈逐项演进 |
| C：Actor / 多进程 / 独立服务 | 高；mailbox、监督、序列化、部署均变化 | 可获得更强故障隔离；IPC/分布式一致性有代价 | 大规模扩展潜力更高；当前桌面研究原型收益未经证明 | 暂不采用 |

最终设计不是把 A、B、C 名词叠加，而是 A 中实际引入已验证的事件触发、操作容量所有权、反馈租约。需要全局语音一致性时再推进 B；只有不合作 provider 或负载证明需要进程边界时再推进 C。

## 4. 下一轮研究协议（尚未执行）

冻结 fixture、硬件、Python/SQLite/SDK、模型版本、语言和测试集；将网络模型延迟与本地开销分开。用统一事件序列比较“原生直接反馈”“应用规则触发”“有界决策+仲裁”，记录无响应、过期、错误打断、重复反馈和遗漏事实。语音准确性由 claim→evidence 人工标注评估，不只看答案是否流畅。

反馈优先级与 cooldown 属于待验证策略，不属于论文已经证明的最佳值。用户实验、长期坚持度和动作表现效果需要独立研究设计与适当伦理/安全审查，本轮没有这些实测结论。

## 5. V2.1：响应身份不等于请求因果，接收不等于播放

本轮再次核查 Qwen 官方客户端/服务端事件及 W3C WebRTC（访问2026-09-23），来源、语言页面日期与实现限制见 [VOICE_OUTPUT_V2_1 第13节](VOICE_OUTPUT_V2_1.md)。

**Fact**：服务端响应带 response.id / response_id，终结有不同 status；所核查创建事件契约没有客户端请求 ID 回传承诺。客户端中文文档对 conversation.item.create 的描述与仓库固定旧 input_text 路径存在差异；未进行在线验证。WebRTC 接收/同步源观察不能单独证明接入播放 sink 或已听见。

**Interpretation**：严格过滤已知 ID 可以减少竞争，但不能把“下一个响应”证明为“这次手动注入的响应”。浏览器收到RTP、服务端生成完毕与实际输出是不同观察。

**Design decision**：不改默认VAD或加入未经验证的wire字段；实现有界 ResponseWindow、原生共享准入、单pending及不确定时阻断；把 ordered candidate 标为 unverified。不伪造 browser playback ACK 或 played_at。

**Trade-off**：隔离提高保守性但损失可用性，ID窗口有有限保护期限；原生12秒lease与0.2秒cancel等待均需真实环境评估。下一阶段应验证单一请求发起模式或受支持的因果关联接口，再设计明确证据强度的浏览器输出观察。
