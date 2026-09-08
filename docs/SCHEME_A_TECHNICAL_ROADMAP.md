# 方案 A 技术路线：确定性运动内核 + Qwen Realtime 表达层

状态：A0/M0 已实现，后续阶段待实施  
基线日期：2026-09-07  
适用入口：`agent_local.py` + `agent_local_agent.py`

## 1. 决策摘要

方案 A 采用“单 Agent、双循环、共享事件总线”的混合架构：

```text
摄像头 30 FPS
    ├─> YOLO Pose 10 FPS -> 平滑/校准 -> 动作 FSM -> CoachEvent
    │                                             ├─> UI（权威状态）
    │                                             └─> FeedbackArbiter
    │                                                    ├─> 本地安全短句
    │                                                    └─> Qwen input_text
    └─> Qwen 视频抽帧 1 FPS -------------------------------> Qwen Realtime

麦克风 16 kHz PCM -> Qwen Realtime <-> 音频回复
                              ^
                              └─ TurnCoordinator：串行化语音轮次与 CoachEvent
```

“单 Agent”表示只保留一个面向用户的教练人格和一个实时会话；“双循环”表示感知/计数循环不依赖 LLM，语言循环不拥有动作真值。这样既保留 Qwen 对音频、视频抽帧和文本的联合理解，也避免把精确计数、安全叫停和节流交给概率模型。

## 2. 本轮代码审查

### 2.1 `agent_local_agent.py`

已经做对的部分：

- `SessionController` 与 Tk UI 解耦，启动、停止、关闭的所有权较清楚。
- YOLO 加载放入 `asyncio.to_thread`，没有直接阻塞 UI 事件循环。
- `Agent`、处理器和本地设备边缘都在 `finally` 中清理。
- `Read @docs/COACHING_INSTRUCTIONS.md` 不是发给模型的普通字符串；Vision-Agents 0.6.9 的 `Instructions` 会读取并展开该 Markdown。
- Qwen 已开启 `include_video=True`，媒体通道具备音频 + 视频抽帧联合输入基础。

主要缺口：

1. YOLO 目前只是通用处理器，没有应用层读取关键点、角度或状态，也没有动作 FSM；“计数”仍由 Qwen 看图后猜测。
2. `session_instructions()` 要求模型“清晰计数”，与方案 A 的“FSM 是唯一真值源”冲突。
3. Vision-Agents 0.6.9 的 Qwen `simple_response()` 主动拒绝文本，导致确定性 `CoachEvent` 暂时无法进入当前实时会话。
4. 服务端 VAD、结构化纠正和模型回复之间没有轮次协调；直接补文本桥会产生双回复或旧纠正补播。
5. 通过 `aiohttp.connector._SSL_CONTEXT_VERIFIED` 修改私有全局变量较脆弱，SDK 升级时必须纳入兼容性测试。
6. 停止动作无法取消正在 `to_thread` 中进行的模型初始化，只能在初始化结束后退出。

### 2.2 `agent_local.py`

已经做对的部分：

- `SessionSettings` 是清晰的会话输入边界。
- 摄像头帧队列只保留最新帧，避免 UI 延迟无限累积。
- 设备发现放在后台线程；UI 生命周期状态比较完整。

主要缺口：

1. UI 只有生命周期状态，没有显示权威的 `phase`、`rep_count`、质量分数、可见性和最后一次纠正。
2. 帧缩放、RGB 转换和 `PhotoImage` 创建在 UI/asyncio 主循环中进行，分辨率或处理帧率升高后可能造成抖动。
3. macOS 找不到 ffmpeg 时直接假设 AVFoundation 设备 `0` 存在；这是可用性兜底，不是可靠探测。
4. 当前日志按字符串包含 “error/失败/ready” 推断级别，后续应消费结构化 UI 事件。

### 2.3 `docs/COACHING_INSTRUCTIONS.md`

已经做对的部分：

- 语言风格、打断要求、安全优先和“只反馈确定看到的”方向正确。
- 动作知识可作为后续规则设计和知识检索的素材库。

必须调整的部分：

1. “没有明确提问时保持安静”与“动作中主动纠正/危险立即制止”冲突。应改成：除高置信动作事件、安全事件和用户提问外保持安静。
2. 指令声称模型在“实时计算角度、识别偏差、精确计数”，但当前没有把这些结构化事实交给模型，容易诱发幻觉。
3. 计数、A/B/C/D 评分和“第几个”的真值必须来自 FSM，LLM 只能复述，不得修改。
4. “记住上次训练”等表述没有持久化实现，当前应删除或标记为只有收到历史数据时才能使用。
5. 676 行全量知识每次进入 realtime session，既增加上下文噪声，也使短句约束更难稳定执行。应拆成短系统契约、动作规则配置和按需知识三层。
6. 单目 2D 姿态不能可靠判断所有疼痛、脊柱中立和关节风险；安全提示必须表达观测边界，不能做医疗诊断。

本阶段不直接重写该指令文件，因为在 FSM 上线前删除模型计数会让现有演示退化。指令迁移安排在 A6，与结构化事件接通同一提交完成。

## 3. 已验证的 Qwen 能力边界

项目实测 `qwen3.5-omni-plus-realtime` 在中国站 WebSocket 端点可以联合消费音频、图片（视频抽帧）和普通 `input_text`。原生 PDF/DOCX 不作为可用输入；文档必须先解析或检索，再转成文本/图片。

需要区分两层：

| 层 | 音频 | 图片/视频抽帧 | `input_text` | 原生 PDF/DOCX |
|---|---:|---:|---:|---:|
| Qwen 模型/API 实测 | 支持 | 支持 | 支持 | 不支持 |
| Vision-Agents 0.6.9 Qwen 适配器 | 支持 | 支持 | 主动拒绝 | 不支持 |

详细证据见 [QWEN_REALTIME_MULTIMODAL_VERIFICATION.md](./QWEN_REALTIME_MULTIMODAL_VERIFICATION.md)。官方入口：

- [Qwen-Omni-Realtime 实时音视频交互](https://help.aliyun.com/zh/model-studio/realtime)
- [Realtime API 客户端事件](https://help.aliyun.com/zh/model-studio/client-events)

## 4. 目标模块与所有权

```text
coach/
├── models.py             # PoseSample、MotionFeatures、CoachEvent、SessionSnapshot
├── geometry.py           # 纯函数：角度、归一化距离、可见性、左右侧选择
├── smoothing.py          # EMA/中值滤波、短时缺点容忍、person_id 连续性
├── calibration.py        # 个人活动范围、镜头方向、阈值派生
├── exercises/
│   ├── base.py           # ExerciseFSM 协议
│   └── squat.py          # 第一项动作：深蹲 FSM
├── arbiter.py            # 优先级、去重、冷却、过期、安全抢占
├── qwen_contract.py      # 已实现：版本与客户端事件契约
├── qwen_bridge.py        # 项目内 input_text 适配；禁止修改 .venv
├── turn_coordinator.py   # 用户轮次/教练事件/response.cancel/音频清空
└── runtime.py            # 把 Pose -> FSM -> Arbiter -> UI/Qwen 串起来
```

核心所有权规则：

- `ExerciseFSM` 唯一拥有 `phase`、`rep_count`、`valid_rep_count` 和质量判定。
- `FeedbackArbiter` 唯一决定一条反馈是否可以发出。
- `TurnCoordinator` 唯一决定何时创建或取消 Qwen response。
- Qwen 只拥有自然语言表达；其输出不得反写动作真值。
- UI 读取 `SessionSnapshot`，不从日志文本反向推断状态。

## 5. 数据契约

### 5.1 姿态样本

```python
@dataclass(frozen=True, slots=True)
class PoseSample:
    timestamp_s: float
    frame_id: int
    person_id: str
    keypoints_xy: tuple[tuple[float, float], ...]   # 归一化到 0..1
    confidences: tuple[float, ...]
    frame_width: int
    frame_height: int
```

原始像素坐标不能直接进入动作规则。所有角度计算同时检查三个关键点置信度；不可见不是“错误姿势”，而是 `visibility_lost`。

### 5.2 动作特征

```python
@dataclass(frozen=True, slots=True)
class MotionFeatures:
    timestamp_s: float
    knee_angle_deg: float | None
    hip_angle_deg: float | None
    torso_lean_deg: float | None
    hip_height_norm: float | None
    knee_track_ratio: float | None
    visibility: float
    side: Literal["left", "right", "front", "unknown"]
```

### 5.3 教练事件

```python
@dataclass(frozen=True, slots=True)
class CoachEvent:
    event_id: str
    kind: Literal[
        "visibility_lost", "phase_changed", "rep_completed",
        "form_warning", "safety_stop", "target_completed"
    ]
    created_at_s: float
    expires_at_s: float
    priority: int                 # safety=100, answer=80, correction=60, praise=20
    dedupe_key: str
    facts: Mapping[str, JSONValue]
    utterance_hint: str | None
```

发给 Qwen 的文本只能由白名单字段序列化，例如：

```json
{
  "schema": "coach.event.v1",
  "kind": "rep_completed",
  "exercise": "squat",
  "rep_count": 3,
  "target_reps": 10,
  "quality": 0.86,
  "instruction": "用一句中文简短确认，不要更改数字"
}
```

## 6. 深蹲 FSM（首个垂直切片）

初始状态机：

```text
UNREADY --稳定可见+校准完成--> STANDING
STANDING --膝角连续下降且越过 enter_down--> DESCENDING
DESCENDING --到达个人深度阈值--> BOTTOM
BOTTOM --膝角连续上升--> ASCENDING
ASCENDING --恢复站立阈值并稳定 N 帧--> STANDING + rep_completed
任意状态 --关键点丢失超过阈值--> UNREADY（不计数）
```

计数门控：

- 必须完整经过 `STANDING -> DESCENDING -> BOTTOM -> ASCENDING -> STANDING`。
- 阈值使用进入/退出双阈值，防止边界抖动。
- 最短/最长动作时长、连续稳定帧和速度方向共同门控。
- 低可见性、换人或时间戳倒退会使本次动作失效。
- 纠正事件不直接决定是否计数；每项质量规则明确 `warning_only` 或 `invalidates_rep`。

## 7. FeedbackArbiter 与打断

优先级从高到低：

1. `safety_stop`：立即抢占；优先固定短句，并取消当前 response。
2. 用户问题：用户开始说话即取消当前模型 response，清空尚未播放的旧音频。
3. `form_warning`：同一 `dedupe_key` 冷却 4–8 秒，只说一项最重要纠正。
4. `target_completed` / `rep_completed`：合并数字与一句鼓励。
5. 普通鼓励：仅在长时间没有更高优先级事件时允许。

所有非安全事件都有 `expires_at_s`。轮次结束时若事件已过期，则丢弃而不是补播。

## 8. 分阶段实施路线

### A0/M0：锁定可复现基线与 Qwen 协议契约（本轮已完成）

改动：

- 精确锁定 `vision-agents==0.6.9`，把直接使用的 `websockets==15.0.1` 写入依赖。
- 将 `uv.lock` 从忽略列表移除，要求后续提交 lockfile。
- 新增 `coach/qwen_contract.py`，集中定义模型、端点、已验证输入和客户端事件构造器。
- `agent_local_agent.py` 显式使用契约中的模型和中国站端点，不再依赖 SDK 隐式默认值。
- 新增无密钥单元测试，校验版本、事件 envelope、输入拒绝和指令文件引用。
- 新增 opt-in 真实 API smoke test，只验证最关键的 `input_text -> response.done` 路径。
- 新增独立的 `scripts/check_local_setup.py`：检查 `DASHSCOPE_API_KEY` 和真实指令路径，且不打印密钥前缀；原有云端 setup 检查保持不变。

验收门：

- `uv lock --offline` 和 `uv sync --locked` 可通过。
- `python -m unittest discover` 全部通过。
- `python scripts/check_local_setup.py` 全部通过且输出中不含密钥片段。
- `RUN_QWEN_LIVE_TEST=1 python scripts/smoke_qwen_realtime.py` 返回 `TEXT_OK`；未设置开关时明确跳过，不产生 API 调用。

### A1/M1：建立领域模型与几何纯函数

修改 `coach/models.py`、`coach/geometry.py`，新增 `tests/test_geometry.py`。先支持 COCO 17 点索引、角度、归一化距离和缺点传播。使用合成关键点测试，不加载 YOLO。

验收门：角度边界、镜像、零长度向量、低置信度和缺点用例全部通过；纯函数不依赖 GUI、网络或 GPU。

### A2/M2：提取 PoseSample、跟踪与平滑

增加应用层 pose sink，将 YOLO 结果转成 `PoseSample`。首版只跟踪一个主人体：按面积、连续中心点和关键点可见性选人；加入 EMA/中值滤波和短时丢点容忍。

验收门：录制夹具重放时人物不会频繁跳换；处理速率不低于 10 Hz；低可见性不产生动作错误事件。

### A3/M3：个人校准

训练开始先收集 3–5 秒稳定站姿和 2–3 次引导动作，得到站立角、可达深度、身体比例、镜头朝向和置信度基线。阈值由默认安全范围和个人范围共同产生。

验收门：校准不足时保持 `UNREADY` 并引导调整镜头；不得用不完整校准计数。

### A4/M4：深蹲 FSM 与离线重放

实现 `coach/exercises/squat.py`，先只支持深蹲。构建正常、半程、抖动、遮挡、中途入镜和两人入镜夹具。

验收门：标注集计数准确率目标不低于 95%；遮挡和半程动作不得误计；相同输入重放结果完全一致。

### A5/M5：FeedbackArbiter

实现优先级、合并、去重、冷却与过期。安全事件使用固定中文模板；普通纠正输出 `utterance_hint`。

验收门：同类错误不会每帧播报；安全事件能抢占；过期事件不会补播。

### A6/M6：QwenTextBridge + TurnCoordinator

项目内包装 Qwen Realtime，而不是修改 `.venv`：通过底层客户端发送已验证的 `conversation.item.create(message/input_text)`，再统一触发 `response.create`。同时处理用户 speech-start、`response.cancel`、本地音频队列清空和事件过期。

此阶段同步重写 `COACHING_INSTRUCTIONS.md`：模型只复述结构化事实，不自行计数、评分或声称看到了未提供的角度。

验收门：用户语音与动作事件同时到达时最多只有一个活动 response；打断后旧音频不再播放；LLM 不能改变 `rep_count`。

### A7/M7：Runtime 与 UI 接线

在 `coach/runtime.py` 组合 pose sink、校准、FSM、arbiter、bridge；在 `agent_local.py` 增加计数、阶段、可见性、最后纠正和连接状态。UI 通过结构化快照更新。

验收门：无网络时确定性计数和 UI 仍可工作；Qwen 断线只影响语言反馈，不破坏会话真值。

### A8/M8：扩展动作与文档知识

按“一个 FSM + 一套录制夹具”逐项增加俯卧撑、平板支撑等。PDF/DOCX 走离线解析、分块、检索后再以 `input_text` 注入；实时阈值和安全规则不得只存在文档里。

## 9. 测试金字塔

| 层 | 是否需要摄像头/密钥 | 覆盖内容 | 每次提交 |
|---|---|---|---:|
| 纯单测 | 否 | 几何、FSM、arbiter、协议 payload | 是 |
| 录制重放 | 否 | 多帧跟踪、计数、质量事件、性能 | 是 |
| SDK 集成 | 否，可使用 fake client | 事件顺序、cancel、过期、断线 | 是 |
| 设备 smoke | 摄像头/麦克风 | 本地采集、播放、关闭 | 发布前 |
| Qwen live smoke | 需要密钥 | 会话、`input_text`、response | 手动/夜间 |
| 端到端训练 | 全部需要 | 真正打断、计数、延迟、恢复 | 发布前 |

首批可观测指标：`pose_fps`、`pose_latency_ms`、`visibility`、`fsm_phase`、`rep_count`、`event_queue_depth`、`event_age_ms`、`qwen_response_active`、`barge_in_latency_ms`。日志只记录 event id 和状态，不记录 API Key、原始音频或未经授权的视频帧。

## 10. 迁移与回滚

- 每个里程碑都由环境变量或构造参数开关，旧的 Qwen 直接视觉路径保留到 A7 端到端通过。
- `rep_count` 一旦切换到 FSM，不允许回退为 LLM 计数；回滚只能回到“无权威计数”。
- QwenTextBridge 若协议失效，关闭语言桥但继续本地计数和 UI。
- SDK 升级必须先更新 `VISION_AGENTS_VERSION`、lockfile、无密钥契约测试和 live smoke 证据，再修改版本约束。

## 11. 下一步建议

下一次提交只做 A1/M1：确定 `PoseSample`/`CoachEvent` 类型，完成几何纯函数和合成数据单测。不要同时接入 QwenTextBridge；桥接必须等 arbiter 与轮次协调器具备后再进入主链路，否则会引入双回复和过期纠正。
