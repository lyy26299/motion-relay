# 教练 Agent 记忆系统实现 Goal

## Goal

在现有 Vision Coach 项目中，建立一个以训练事实为核心、可查询、可追溯、可删除的记忆系统，并把它接入教练 Agent 的实际工作流，使 Agent 能像现实中的健身教练一样：训练前了解用户和历史，训练中观察并及时纠正，组间根据表现调整指导，训练后总结并在下次训练中使用这些经验。

首个可交付版本只做一个用户档案、单人固定机位和深蹲垂直切片。系统必须完成以下闭环：

```text
YOLO 姿态观测
  -> 本地动作特征与深蹲 FSM
  -> 当前训练的 Working Memory
  -> SQLite 长期事实账本
  -> 结构化查询 / MCP / RAG
  -> Agent 决策与教练反馈
  -> 反馈播放结果与后续动作变化
  -> 训练结束摘要，成为下一次训练可用的记忆
```

记忆系统不是给 Qwen 增加“请记住”提示词，也不是把每帧关键点直接塞进上下文。所有数字、次数、动作质量和训练统计必须来自可重放的本地事实链路；模型只负责理解、查询规划、解释和表达。

## 现实教练工作流映射

| 现实中的教练动作 | 系统行为 | 记忆来源 |
| --- | --- | --- |
| 训练前询问目标、限制和偏好 | 读取当前用户档案，确认本次动作、目标次数、机位和授权范围 | `profile_facts` |
| 热身和观察动作 | 持续接收姿态快照，判断可见性、主人体和当前动作阶段 | Working Memory |
| 动作中即时纠正 | 本地规则发现稳定问题后，经反馈仲裁器发一句短提示；不等待 RAG，不阻塞计数 | 当前快照、动作事件 |
| 组间复盘 | 汇总本组次数、有效次数、动作时长、范围、问题和观测质量 | `reps`、`events` |
| 根据表现调整训练 | Agent 查询历史并提出训练计划变更；用户确认后才修改计划 | 结构化训练事实、情节记忆 |
| 训练后总结 | 由确定性统计生成摘要候选，模型只能将已有事实组织成文字并保留证据引用 | `session_summaries` / `memory_notes` |
| 下次训练回顾 | 按用户、动作、时间和可比较条件检索历史，回答“上次怎样”“为什么提醒我” | SQL + FTS / 向量检索 |
| 用户报告疼痛或不适 | 立即暂停本地训练和输出，记录用户自述；不得从 2D 姿态推断疼痛或医疗结论 | 安全状态 + 可选用户事实 |

## 范围与不做事项

### 本 Goal 必须实现

1. `PoseSnapshot -> MotionSnapshot -> CoachEvent/RepRecord` 的结构化事实链路。
2. 有界短期记忆：当前快照、最近姿态窗口、最近事件、当前对话和待执行任务。
3. SQLite 长期账本：用户、训练会话、动作、反馈、用户档案、摘要、outbox。
4. 幂等写入、事务恢复、用户 scope 隔离、删除和缓存失效。
5. 只读记忆工具和审核知识检索工具；模型不能执行任意 SQL、文件或网络操作。
6. 有限步 Agent Loop：`Observe -> Retrieve -> Decide -> Act -> Observe`。
7. 训练事实、证据 ID、规则版本和反馈播放状态可追溯。
8. 与现有 `SessionController`、`StructuredPoseProcessor` 和 `DuplexQwenRealtime` 的真实接入。

### 本 Goal 不实现

- 不保存原始音视频，除非用户明确开启额外保留策略。
- 不把未经本地动作内核确认的 Qwen 观察写成训练事实。
- 不让 LLM 拥有权威计数、暂停状态、用户身份或训练计划的直接写权限。
- 不从 YOLO 关键点推断疼痛、受伤、医疗风险或“动作安全”。
- 不在首版同时扩展五种动作、多人训练、云端多租户或模型微调。
- 不用向量检索代替次数、角度、时长等精确数值查询。

## 目标架构

### 数据所有权

- `StructuredPoseProcessor`：产出所有检测人体、关键点、置信度、时间和投影角度；不决定动作合格。
- `MotionRuntime / SquatFSM`：唯一拥有阶段、完成次数、有效次数和规则判定。
- `WorkingMemory`：保存当前会话的有界窗口、状态版本、暂停锁存和活动任务。
- `MemoryService`：保存和读取事实、摘要、授权、删除和索引版本；不提升模型猜测为传感器事实。
- `AgentLoop`：组织查询、证据和决策；只能使用允许的工具和结构化输入。
- `TurnCoordinator / DuplexQwenRealtime`：管理语音轮次、取消、插话和播放状态；不能修改动作事实。
- UI：展示权威的本地次数、有效次数、阶段、暂停原因和记忆写入状态。

### 推荐模块

```text
coach/
  models.py                 # PoseSnapshot、MotionSnapshot、Event、RepRecord
  pose_adapter.py           # 现有结构化 YOLO 入口
  geometry.py
  exercises/squat.py        # 可重放深蹲 FSM
  working_memory.py         # 有界短期状态
  runtime.py                # 姿态、动作、事件的本地运行时
  memory/store.py            # SQLite WAL、事务、参数化查询
  memory/policy.py           # 授权、scope、保留期、删除
  memory/consolidate.py      # 组末统计、摘要候选、outbox
  memory/retrieval.py        # SQL + FTS + 可选向量检索
  agent_loop.py              # 有限步 Observe/Retrieve/Decide/Act
  turn_coordinator.py        # 单活动 response、generation、播放清理
  qwen_bridge.py             # 复用 qwen_duplex 的文本/工具桥
  mcp_server.py / mcp_client.py
```

## 记忆模型

### 短期记忆

- 当前实时快照：阶段、次数、有效次数、可见性、暂停原因、计划版本、状态版本。
- 姿态环形窗口：默认保留最近 30 秒，仅用于特征计算和短期证据。
- 最近事件：最多 100 条且最多 2 分钟；安全暂停单独锁存，不随 TTL 自动解除。
- 对话窗口：最近 8 轮或 2,000 token，只保留最终转写和可用的教练输出。
- 当前任务：每轮一个 `turn_id`、`generation`、截止时间、工具预算和 evidence refs。

### 长期记忆

1. **训练事实**：会话、组、每次动作、动作质量、角度/时长、缺失比例、规则版本。
2. **情节记忆**：发生了什么问题、教练说了什么、是否播放、后续动作是否出现可观察变化。
3. **用户档案**：目标、偏好、训练经验和用户明确自述的限制；有来源、确认时间和有效期。
4. **教练知识**：经审核的动作规则、适用条件、观测限制和文档版本；与私人记忆严格分区。

模型摘要是派生数据，必须保存来源事件和 revision，不能替代原始训练事实。

## 最小数据契约

所有事件至少包含：

```json
{
  "schema_version": "coach.event.v1",
  "event_id": "evt-...",
  "user_id": "profile-local-01",
  "session_id": "session-...",
  "sequence": 108,
  "captured_at_utc": "...",
  "capture_mono_ms": 184230,
  "kind": "rep_completed",
  "source": {
    "type": "motion_fsm",
    "model": "yolo11n-pose",
    "rule_version": "squat-v1",
    "calibration_id": "cal-..."
  },
  "facts": {
    "exercise": "squat",
    "set_index": 1,
    "rep_index": 3,
    "valid": false,
    "duration_ms": 3100,
    "knee_angle_min_deg": 98.2,
    "usable_sample_ratio": 0.94,
    "reason_codes": ["below_calibrated_range"]
  },
  "evidence_refs": ["pose-window-..."]
}
```

关键规则：非观测值使用 `null`，不能填 0；每次动作必须可关联规则版本和证据；重复事件按 `(session_id, sequence)` 或幂等键去重；跨会话用 `user_id`，跨帧人体身份使用临时 track，不得把检测数组下标当作用户身份。

## Agent 实际接入流程

### 1. 训练开始

`SessionController` 创建 `user_id`、`session_id`、`session_epoch` 和 `plan_version`，读取用户档案与最近同动作训练摘要，建立 `WorkingMemory`。用户确认动作、目标次数和机位后才开始计数。

### 2. 训练中

`StructuredPoseProcessor` 继续输出视频和 `PoseSnapshot`。本地动作内核消费快照并产生 `MotionSnapshot`、`rep_completed`、`form_issue`、`visibility_lost` 等事件。事件一份更新 UI 和 Working Memory，另一份进入异步账本队列；数据库、MCP、embedding 或 Qwen 变慢不能阻塞姿态采集和安全暂停。

稳定的实时纠正由 `FeedbackArbiter` 根据优先级、冷却和事件时效选择；动作中默认使用本地审核话术。用户问历史、组间复盘或计划调整时，才启动 Agent Loop。

### 3. Agent Loop

```text
Observe
  读取不可变 WorkingMemory 快照，绑定 session_epoch / turn_id / state_version / deadline
Retrieve
  先查结构化训练事实，再按需查情节记忆和审核知识
Decide
  输出 answer / cue / propose_plan / ask_clarification / abstain
Validate
  校验 action、参数、证据、scope、计划版本和预算
Act
  通过 TurnCoordinator 输出或提交待确认计划
Observe
  记录输出是否生成、入队、被打断、播放完成，以及后续动作统计
```

初始限制：每轮最多 2 次模型决策、每次最多 2 个工具调用；普通查询 4 秒截止，实时纠正 2 秒 TTL；用户插话、停止、会话变化或证据过期后，旧 generation 的工具结果和音频一律丢弃。

### 4. Qwen 连接

使用现有 `DuplexQwenRealtime` 的取消和音频清理能力，新增受控文本/工具桥：

```text
最终转写
  -> AgentLoop 查询与校验
  -> conversation.item.create(input_text 或 function_call_output)
  -> TurnCoordinator 决定 response.create
  -> Qwen 音频
  -> 播放状态回写 feedback
```

如果当前 Qwen 端点无法可靠支持“先工具查询、后 response”，首版只能承诺会话开始或组间预加载的历史，不能宣称每个新问题都完成了即时记忆查询；此兼容性必须先用契约测试验证。

## MCP 与检索边界

首版使用本机 stdio MCP。工具由应用绑定当前用户 scope，模型不能传入任意 `user_id`：

- `memory.get_profile`：读取已确认、未过期的档案事实。
- `memory.query_training`：按动作、时间和指标查询精确统计，参数化 SQL，`limit <= 20`。
- `memory.search_episodes`：检索用户情节记忆，先做 scope 过滤，`top_k <= 5`。
- `memory.get_evidence`：读取本轮允许的证据 ID，并返回 expired/unavailable 状态。
- `knowledge.search`：只检索审核知识，返回章节、版本和适用条件。
- `profile.propose_update`：只生成用户档案变更提案，不直接提交。

数字问题优先 SQL；“上次你为什么提醒我”“什么提示对我有帮助”等问题才使用 FTS/向量检索。检索无结果时必须明确说明没有依据，不得补全历史。

## 隐私、授权和删除

- 默认不保存原始音视频；姿态数据默认只保留短窗口和动作统计。
- 用户档案、健康自述和情节摘要按独立授权范围保存。
- 删除必须同时失效 SQLite 事实、摘要、FTS/向量索引、缓存、待执行 outbox 和新会话预加载上下文。
- 每个后台摘要/embedding 任务写入前复查 `memory_epoch`，防止删除后迟到结果复活。
- 日志只记录 ID、版本和状态，不记录 API Key、原始音频或未经授权的视频帧。

## 分阶段交付

### Phase 1：动作事实基础

新增深蹲 FSM、主人体/多人暂停、校准、`MotionSnapshot` 和幂等动作事件。UI 显示权威次数与有效次数。验收：10 次动作中 8 次有效时，账本和 UI 都是 `completed=10, valid=8`。

### Phase 2：短期记忆与安全反馈

新增 `WorkingMemory`、反馈仲裁和 watchdog。断云时仍可本地计数和暂停；用户说“膝盖疼”立即暂停，不能等待模型或检索。

### Phase 3：长期账本

新增 SQLite WAL、事务写入、恢复、档案授权、删除和组末整理。重启后可查询上次训练；重复事件不重复计数；写入失败时显示 pending/failed，不显示“已保存”。

### Phase 4：Qwen 受控轮次

新增 `TurnCoordinator` 和 Qwen 文本桥，验证最终转写到检索再到回答；插话后旧工具结果、旧文本和旧音频不再执行或播放。

### Phase 5：MCP、RAG 与 Agent Loop

实现真实 stdio MCP 往返、结构化查询、中文 FTS/向量混合检索、证据引用和计划提案。模型只能在白名单工具、参数 schema 和预算内行动。

### Phase 6：真实质量验证

使用按用户和会话隔离的真人标注数据验证计数 precision/recall、计数误差、RAG Recall@5、历史回答证据正确率、首音延迟、插话取消和 30 分钟运行稳定性。没有真实数据时，只能报告合成测试通过，不能宣称教练质量达标。

## 验收标准

1. 完成 10 次深蹲，其中 2 次不满足当前规则，长期账本保存 `completed=10, valid=8`，每次动作都有指标、规则版本和证据引用。
2. 用户询问“刚才第 3 个为什么提醒我”，Agent 能从动作/反馈 ID 定位原因和数值，不依赖重看视频。
3. 重启后询问“比上次稳定吗”，Agent 只比较同动作、同视角/规则等可比较样本；无匹配历史时明确回答无依据。
4. 用户说“膝盖疼”时，安全暂停优先级高于检索、总结和语音模型；暂停不会被过期事件解除。
5. 第二人进入画面、主人体不明确、帧过期或换轨时暂停计数，不把两个人拼成一次动作。
6. 用户删除训练历史后，数据库、检索索引、缓存、outbox 和新会话上下文都不再使用这些数据。
7. 插话或停止后，旧 generation 的工具结果、计划提案和音频不会落地；播放状态区分 generated、queued、interrupted、completed、unknown。
8. 所有测试可在无摄像头、无云端或无 embedding 的环境下验证核心账本和 Agent Loop 契约。

## 给实现 Agent 的执行要求

请在当前仓库内按 Phase 1 到 Phase 5 实现，不重建媒体层，不把 Qwen 视觉输出当作动作真值。每个阶段都要：

1. 先阅读现有 `coach/`、`SessionController`、Qwen 契约和相关文档，保持现有接口兼容。
2. 以小步提交实现，新增类型和模块优先于侵入式重写。
3. 为数据契约、幂等写入、删除隔离、插话取消和过期决策添加自动化测试。
4. 运行现有测试与新增测试；不能连接真实设备或 API 时，明确标记未验证边界。
5. 最终报告改动文件、已通过的验收项、尚未实现的能力和下一阶段阻塞项，不把设计文档中的目标写成已经完成。

完成本 Goal 的标准是：教练 Agent 能基于有来源的当前事实和历史记忆完成一次可解释的训练闭环，而不是只在提示词中声称“记住了用户”。
