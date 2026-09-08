# 实时多模态健身教练 Agent 架构设计

版本：v1.0

定位：在现有 `agent_local.py`、`agent_local_agent.py`、`coach/` 和 Qwen Realtime 媒体链路之上，构建一套可落地的 Talker--Reasoner Agent。本文是目标架构、实现边界和验收标准；“已实现”和“待实现”明确区分。

## 1. 设计目标与边界

### 1.1 目标

系统面向固定机位、单人、居家训练场景，完成：

~~~text
摄像头/麦克风
    -> 多模态实时交互
    -> 姿态观测
    -> 本地动作事实
    -> 快速反馈
    -> 历史查询与训练建议
    -> 训练结果沉淀
~~~

核心目标不是让大模型“看起来像记住了用户”，而是让 Agent 能够：

1. 在动作进行时保持低延迟，不等待慢速检索或长链路推理。
2. 将次数、动作阶段、安全暂停和质量判定交给可重放的本地程序。
3. 让大模型基于有来源的事实进行解释、规划和自然语言表达。
4. 在插话、停止、断网、多人入镜、帧超时和工具错误时不执行过期动作。
5. 对历史训练、用户偏好和教练知识进行隔离、授权、查询和删除。

### 1.2 首版范围

- 一个本地用户档案、单人固定机位。
- 先实现深蹲垂直切片，再扩展其他动作。
- 默认不保存原始音频和视频，只保存必要动作统计及授权记忆。
- 不从 2D 姿态推断疼痛、受伤、医疗风险或动作安全结论。
- 没有真实标注数据时，只报告逻辑回放结果，不宣称真人识别准确率。

### 1.3 当前代码状态

当前已经具备 Fast Runtime 的主要基础：

- `StructuredPoseProcessor`：一次推理输出视频帧和结构化 `PoseSnapshot`。
- `geometry.py`：计算经过像素比例校准的投影角度。
- `SquatFSM`：产生完成次数、有效次数和 `RepRecord`。
- `MotionRuntime`：去重、watchdog、事件分发和短期状态更新。
- `WorkingMemory`：有界姿态/事件/对话窗口、状态版本和暂停锁存。
- `memory/store.py`：SQLite 事实账本、幂等写入、事务和删除接口。
- `DuplexQwenRealtime` 与 `qwen_contract.py`：Qwen Realtime 音视频契约、打断和音频清理。
- BrowserEdge：浏览器采集、WebRTC 传输、AEC 检查和关闭清理。

完整的 Slow Reasoner、真实 MCP 往返、混合 RAG 和受控 Qwen 文本桥仍需分阶段实现，不能从现有媒体链路推断这些能力已经上线。

## 2. 总体架构

~~~mermaid
flowchart TB
    User[用户]
    Browser[浏览器摄像头/麦克风/AEC]
    Front[Front Agent / Talker<br/>Qwen Omni Realtime<br/>视觉·语音·对话·轮次·打断]
    Fast[Fast Runtime<br/>10 Hz 本地循环]
    Pose[Pose Adapter<br/>YOLO Pose]
    Motion[Motion Runtime<br/>Geometry + FSM + Safety]
    State[(Shared State<br/>版本化会话状态)]
    Arbiter[Feedback Arbiter<br/>优先级·冷却·过期]
    Reasoner[Slow Reasoner<br/>规划·用户建模·历史分析·反思]
    Tools[受控工具层<br/>Memory / Knowledge / Profile]
    Ledger[(SQLite Fact Ledger)]
    Index[(FTS / Vector Index)]
    UI[本地 UI<br/>权威状态]

    User <--> Browser
    Browser <--> Front
    Browser --> Fast
    Fast --> Pose --> Motion --> State
    Motion --> Arbiter --> Front
    State --> UI
    State --> Reasoner
    Reasoner --> Tools
    Tools --> Ledger
    Tools --> Index
    Motion --> Ledger
    Reasoner --> Front
    Front --> Browser
~~~

三个循环的速度和职责不同：

| 层 | 主要职责 | 典型频率/触发 | 是否允许等待 LLM |
| --- | --- | --- | --- |
| Front Agent / Talker | 音频、视频、对话、语音、轮次和打断 | 持续媒体流、用户说话 | 只等待实时模型自身的流式输出 |
| Fast Runtime | 姿态、几何、动作 FSM、计数、安全、当前状态 | 目标 10 Hz | 不允许 |
| Slow Reasoner | 历史查询、计划、用户建模、长期分析、反思 | 用户问题、组末、会话结束 | 允许，但有明确预算 |

### 2.1 两种架构的关系

- **Talker--Reasoner** 是应用层架构：Talker 处理快速交互，Reasoner 负责慢速多步骤推理。
- **Thinker--Talker** 是 Qwen Omni 类模型内部的理解/语音生成结构。
- 本项目的 Front Agent 使用 Qwen Omni Realtime 作为表达层，但不把模型内部的 Thinker 当作动作真值来源。
- Fast Runtime 是 Talker 与 Reasoner 之间的可信事实边界；模型只能读取筛选后的状态和工具结果。

## 3. 模块职责与数据所有权

| 组件 | 唯一拥有的行为/数据 | 明确不拥有 |
| --- | --- | --- |
| BrowserEdge | 浏览器媒体采集、AEC、音频播放 | 姿态判断、用户历史 |
| Front Agent | 多模态会话、转写、语音输出、打断 | 权威计数、用户身份写入 |
| Pose Adapter | 输入帧、全部人体关键点、框、置信度、时间 | 动作合格结论、长期身份 |
| Geometry | 角度、距离、可见性等纯函数结果 | 阈值策略、自然语言 |
| MotionRuntime/FSM | 阶段、完成次数、有效次数、规则判定 | 任意 SQL、模型调用 |
| FeedbackArbiter | 反馈优先级、冷却、去重、过期检查 | 创建并发模型 response |
| WorkingMemory | 当前会话窗口、状态版本、暂停锁存、活动任务 | 跨用户共享缓存 |
| MemoryService | 事实账本、授权、删除、索引版本 | 把模型猜测升级为传感器事实 |
| Slow Reasoner | 查询计划、证据组织、建议和反思 | 直接写计数、任意代码/SQL |
| MCP Server | 白名单工具和参数 schema | 通用文件系统、任意网络、任意 SQL |
| UI | 展示权威状态和错误状态 | 从日志文本推断状态 |

关键不变量：

1. `PoseSnapshot -> MotionSnapshot -> CoachEvent/RepRecord` 是唯一动作事实链路。
2. LLM 输出不能覆盖 `completed_reps`、`valid_reps`、`phase`、`paused` 或 `user_id`。
3. 所有后台任务携带 `session_epoch`、`turn_id`、`state_version` 和 `generation`。
4. 过期、被打断或删除 epoch 不匹配的结果必须丢弃。
5. 精确数字优先走结构化查询；向量检索不能替代次数、角度和时长查询。
6. 不可观测值使用 `null` 或显式状态，不能用 0 或模型猜测填充。

## 4. Fast Runtime：快速、确定性的动作循环

### 4.1 数据流

~~~text
VideoFrame
  -> YOLO 11 Pose
  -> 全量检测转换（CPU、不可变）
  -> 可见性/多人判断
  -> 几何特征（膝/髋角度）
  -> MotionRuntime
  -> SquatFSM
  -> CoachEvent / RepRecord
  -> SharedState + UI + Ledger Queue
~~~

目标频率为 10 Hz。摄像头可以高于 10 FPS，但姿态推理只保留一个待处理帧，避免无界积压。

### 4.2 PoseSnapshot 要求

- 张量在工作线程内复制到 CPU，跨线程不持有 GPU tensor。
- 坐标可归一化存储，但角度计算必须恢复宽高比。
- 所有检测目标都保留；多人时不选择一个人冒充主用户。
- `observed_at` 与 `processed_at` 分开记录，便于拆分排队延迟和推理耗时。
- `stream_epoch` 变化后，旧轨道结果不得写入新会话。
- `status` 至少区分 `observable`、`partial`、`no_person`、`multiple_people` 和 `inference_error`。

### 4.3 深蹲 FSM

~~~text
unknown -> standing -> descending -> bottom -> ascending -> standing
                \-> paused（多人、无人物、超时或推理错误）
~~~

规则：

- 站立角度、底部角度和最短动作时长均为版本化配置。
- 使用滞回，避免角度在阈值附近抖动造成重复切换。
- 进行中的动作遇到多人、丢点或超时直接作废，不跨异常区间拼接。
- `RepRecord` 记录动作时长、最小/最大膝角、可用样本比例、原因码和证据帧范围。
- FSM 只表达观测规则，不表达医疗安全结论。

### 4.4 Feedback Arbiter

反馈优先级：

~~~text
安全暂停 > 可见性恢复 > 用户明确问题 > 稳定动作纠正 > 组末总结 > 鼓励
~~~

仲裁条件：

- 同一 `dedupe_key` 在冷却时间内只发送一次。
- 实时纠正 TTL 建议为 2 秒；事件过期不补播。
- 任何语音输出都关联 `event_id` 和 `generation`。
- 用户插话时先清理待播音频，再取消旧 response 和旧任务。
- Fast Runtime 不等待 RAG、embedding 或 Qwen；云端不可用时仍能计数和暂停。

## 5. Front Agent：实时 Talker

输入：

- 浏览器上行 PCM 音频。
- 浏览器视频抽帧。
- Fast Runtime 经过白名单序列化的事件文本。
- Slow Reasoner 返回的受证据约束的回答上下文。

输出：

- Qwen Realtime 流式音频。
- 用户转写和 Agent 语音转写。
- `generated / queued / interrupted / completed / failed` 播放状态。

轮次流程：

~~~text
用户语音结束
  -> final_transcript
  -> Route
       动作纠正/安全事件：Fast Runtime 直接处理
       历史问题/计划调整：提交 Slow Reasoner
  -> 通过一个活动 response 输出
  -> 记录播放与打断结果
~~~

Front Agent 不直接决定是否查询历史；TurnCoordinator 负责把用户问题路由到 Fast Runtime 或 Slow Reasoner。

当前 Vision-Agents 0.6.9 的 Qwen 适配器对 `input_text` 存在边界，因此需要项目内文本桥。文本桥先通过 `qwen_contract.py` 构造和校验事件；如果端点无法可靠支持“先工具、后回答”，首版降级为会话开始/组间预加载历史，不宣称每个新问题都实时完成查询。

## 6. Slow Reasoner：慢速规划与长期分析

### 6.1 触发条件

- 用户询问上次训练、动作原因或历史变化。
- 组结束或训练结束，需要生成带证据的总结。
- 用户请求调整训练计划。
- 多次动作统计达到分析窗口。
- 用户明确提出偏好或限制，需要形成候选档案变更。

每帧不触发 Reasoner，每次普通动作纠正也不触发 Reasoner。

### 6.2 有限步 Agent Loop

~~~text
Observe
  读取 WorkingMemory，绑定 session_epoch / turn_id / state_version / deadline
Route
  判断安全、实时纠正、历史查询、计划提案或拒答
Retrieve
  先查结构化事实，再按需检索情节记忆和审核知识
Decide
  输出结构化 CoachDecision
Validate
  校验 action、参数、scope、证据、预算和计划版本
Act
  交给 TurnCoordinator 输出；计划变化只提交 proposal
Observe
  记录生成、排队、打断、播放和后续动作变化
~~~

初始预算：

- 每轮最多 2 次模型决策。
- 每次决策最多 2 个工具调用。
- 单个本地工具软目标 300 ms。
- 普通历史查询截止时间 4 s。
- 实时纠正事件 TTL 2 s。
- 用户插话、停止、会话 epoch 变化或证据过期后，旧 generation 一律失效。

允许的决策：`answer`、`cue`、`propose_plan`、`ask_clarification`、`abstain`。Reasoner 不返回任意 SQL、任意代码或未经证据约束的数字。

## 7. MCP、RAG 与记忆

### 7.1 工具白名单

| 工具 | 用途 | 权限边界 |
| --- | --- | --- |
| `memory.get_profile` | 读取已确认且未过期的用户偏好 | 当前用户 scope，只读 |
| `memory.query_training` | 查询次数、角度、时长等精确数据 | 参数化 SQL，`limit <= 20` |
| `memory.search_episodes` | 查询情节记忆和反馈结果 | 先做用户过滤，`top_k <= 5` |
| `memory.get_evidence` | 获取动作/事件证据 | 只允许本轮 evidence ID |
| `knowledge.search` | 查询审核后的教练知识 | 只读 approved 文档 |
| `profile.propose_update` | 生成偏好变更建议 | proposal，不直接提交 |

模型不能传入任意 `user_id`，不能调用通用文件系统、shell、网络或 SQL 工具。

### 7.2 检索策略

~~~text
数字问题：SQLite 参数化查询
事实解释：SQLite + evidence_refs
“什么提示更有帮助”：关键词/向量混合检索
教练规则：审核知识库检索
无结果：明确返回“当前没有依据”，禁止补全历史
~~~

Embedding 只用于语义召回，不用于替代精确统计。索引记录模型、维度、归一化、距离算法和 revision；删除用户数据时同时失效 SQLite、FTS、向量、缓存和 outbox。

### 7.3 记忆分层

| 数据 | 默认生命周期 | 用途 |
| --- | --- | --- |
| 当前快照 | 1 条 | UI 和即时决策 |
| 姿态窗口 | 30 秒 | 平滑和证据窗口 |
| 最近事件 | 2 分钟/100 条 | 反馈和短期追问 |
| 对话窗口 | 8 轮或 2,000 token | 当前会话上下文 |
| 动作事实 | 用户授权的长期记录 | 精确查询和比较 |
| 情节记忆 | 用户授权的长期记录 | 查询提示效果和训练过程 |
| 教练知识 | 版本化、审核后长期保存 | 规则解释和适用条件 |

写入规则：

- 事件和动作记录使用幂等键，重复回放不重复累加。
- 动作事实先落盘，摘要和 embedding 通过 outbox 异步生成。
- 数据库写入失败时状态为 `pending/failed`，不能显示“已保存”。
- 删除操作递增 `memory_epoch`；后台任务写入前复查 epoch。
- 用户、会话、事件、索引和缓存均按 scope 隔离。

## 8. 并发、延迟与取消

### 8.1 队列设计

~~~text
BrowserEdge frame queue       max=1，最新帧优先
Pose worker                  1 个，串行调用模型
Motion event queue           有界，不能丢失 rep_completed
Reasoner task                每会话最多 1 个活动任务
Audio playout queue          可清空，打断时立即 flush
Outbox queue                 持久化任务，幂等重试
~~~

### 8.2 取消顺序

取消触发源：用户插话、停止训练、窗口关闭、会话重启、删除记忆、deadline 超时。

1. 标记当前 `generation` 失效。
2. 停止接受旧任务的新结果。
3. 取消 Reasoner 工具任务和 Qwen response。
4. 清空待播音频。
5. 保留已提交事实，未提交结果标记为 pending/unknown。
6. 释放媒体、处理器和浏览器资源。

`asyncio` 取消不能强制终止已经进入原生模型推理的线程；模型加载需有结果接管和最终清理路径，必要时放入独立进程。

### 8.3 延迟预算

| 路径 | 目标 | 超时后的行为 |
| --- | --- | --- |
| 姿态输入到 Fast Runtime | p95 由设备实测决定 | 丢旧帧，不堆积 |
| 实时纠正 | 2 s TTL | 使用固定短句或放弃 |
| 本地历史工具 | 缓存 p95 <= 100 ms | 使用旧快照并标记 as-of |
| 普通历史问答 | 4 s | 返回可解释的超时 |
| 语音首包 | 以真实端点实测为准 | 不阻塞计数 |

## 9. 故障与安全降级

| 故障 | Fast Runtime | Front Agent | Reasoner |
| --- | --- | --- | --- |
| 无人物/多人 | 暂停计数并锁存原因 | 播放短提示 | 不启动历史推理 |
| 帧超时 | watchdog 产生 visibility_lost | 提示重新站位 | 丢弃依赖当前状态的任务 |
| YOLO 异常 | 输出 inference_error，不伪造角度 | 保持会话或提示重试 | 不生成动作结论 |
| Qwen 断开 | 继续本地计数和安全暂停 | 进入离线状态 | 可查询本地账本 |
| MCP/RAG 超时 | 不受影响 | 使用本地固定短句 | 返回工具超时 |
| 用户说疼痛 | 立即暂停并记录自述 | 简短回应，不做诊断 | 不自动改变医学结论 |
| 旧 generation 返回 | 丢弃 | 不播放 | 不写状态 |
| 记忆删除请求 | 停止相关写入 | 重建会话上下文 | 取消旧任务并递增 epoch |

## 10. 推荐代码结构

~~~text
coach/
  models.py                 # PoseSnapshot、MotionSnapshot、事件和动作记录
  pose_adapter.py           # YOLO 推理与结构化转换
  geometry.py               # 纯几何和可见性函数
  exercises/squat.py        # 深蹲 FSM
  runtime.py                # Fast Runtime 编排
  working_memory.py         # 有界短期状态
  arbiter.py                # 反馈优先级、冷却、去重、过期
  browser_edge.py           # 浏览器 WebRTC/AEC
  qwen_duplex.py            # Qwen Realtime 媒体适配
  qwen_contract.py          # Qwen 事件契约
  turn_coordinator.py       # response、取消、播放状态
  qwen_bridge.py            # input_text/function result 文本桥
  agent_loop.py             # Slow Reasoner 主循环
  memory/store.py           # SQLite WAL、事务、幂等、删除
  memory/retrieval.py       # SQL、FTS、向量检索
  memory/policy.py          # scope、授权、保留期
  memory/consolidate.py     # 组末统计、摘要、outbox
  mcp_server.py             # 只读和受限 proposal 工具
  mcp_client.py             # 应用绑定 scope 的客户端
~~~

## 11. 分阶段落地计划

### Phase 0：基线和协议

锁定 Python、Vision-Agents、Qwen endpoint 和浏览器媒体契约。验收缺密钥、无设备、停止和关闭均有明确状态；Qwen 音视频输入、打断和 AEC 冒烟可复现。

### Phase 1：Fast Runtime 垂直切片

完成 PoseSnapshot、Geometry、深蹲 FSM、MotionRuntime 和 WorkingMemory。验收 10 个完整动作中 2 个浅蹲时，`completed=10, valid=8`；多人、超时、重复帧和换轨不产生错误计数。

### Phase 2：事实账本

完成 SQLite WAL、事务、幂等、恢复、用户 scope 和删除。验收重复事件只写一次；重启可查询；删除后缓存、索引和 outbox 不再使用旧数据。

### Phase 3：FeedbackArbiter 与受控 Talker

完成事件优先级、冷却、过期、response.cancel、音频清理和状态回写。验收动作中纠正不等待 Reasoner；插话后旧音频不播放；云端断开不影响本地计数。

### Phase 4：Slow Reasoner 与 MCP

完成 Observe/Route/Retrieve/Decide/Validate/Act/Observe，接入白名单 MCP 工具。验收每轮最多 2 次决策、每次最多 2 个工具；任意 SQL、任意用户 scope 和过期证据均被拒绝。

### Phase 5：RAG 与长期个性化

完成审核知识库、中文 FTS/向量混合召回、情节记忆、计划提案和反馈结果关联。验收数值问题不走向量近似；检索结果有 evidence ID、版本和适用条件；用户确认后才修改计划。

### Phase 6：真实质量评测

使用真人标注回放、真实设备和长时间运行验证：

- 动作计数 precision/recall、计数误差和拒判覆盖率。
- 历史回答证据正确率、RAG Recall@5。
- 首音延迟、插话取消成功率、过期执行拦截率。
- 30 分钟运行期间的内存曲线、队列长度和异常恢复率。

## 12. 可行性结论

该方案可行，但必须按“Fast Runtime 先于 Slow Reasoner”的顺序落地：

1. 姿态、FSM、事实账本和短期状态是确定性基础，当前代码已经覆盖大部分第一阶段。
2. Front Agent 可以继续复用 Qwen Realtime 和 BrowserEdge，不需要重建媒体层。
3. Slow Reasoner 不参与每帧动作判断，只在历史问题、组末总结和计划调整时运行。
4. MCP 是受控工具协议，不是存储层；数据库和检索服务负责权限、幂等和删除。
5. RAG、embedding 和模型总结都是事实账本之后的派生能力，不能覆盖传感器事实。
6. 真实质量需要真人标注和设备测试；合成数据只能证明状态机和数据契约逻辑。

最终验收标准不是“模型能说话”，而是：在媒体抖动、用户插话、多人入镜、云端不可用和历史删除等情况下，系统仍能保持动作事实正确、反馈边界清晰、任务可取消、结果可追溯。

