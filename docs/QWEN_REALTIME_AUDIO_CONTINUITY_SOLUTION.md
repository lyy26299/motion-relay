# Qwen Realtime 话语分段与语音停顿治理方案

状态：Phase 1 已实现；Turn Coordinator 与真实云端验收待完成  
日期：2026-09-08  
适用版本：`vision-agents==0.6.9`、`qwen3.5-omni-plus-realtime`、本地 `BrowserEdge`

### 本轮实现结果

已完成：

- 默认启用 Qwen 3.5 官方推荐的 `semantic_vad`，并开放 VAD 类型、阈值和静音时长配置；
- 将默认 VAD 从 `server_vad / 0.35 / 600 ms` 调整为 `semantic_vad / 0.20 / 900 ms`；
- 将下行播放器改为显式 `IDLE / PRIMING / PLAYING / REBUFFERING` 状态机；
- 修复首次播放误用恢复阈值、短回答不足阈值无法启动、正常 final 被计作 underrun 等问题；
- 使用 `response.audio.done` 及时排出重采样尾部，并过滤取消响应的迟到 audio/done 事件；
- 增加 response 音频块数、音频时长、最大 delta 间隔和播放状态转换日志；
- 新增 VAD 合约、预缓冲、短回答、重缓冲和取消响应竞态测试。

尚未完成：

- server VAD 与应用 `inject_text()` 之间的单一响应所有权；
- 真实 DashScope 会话下的分段率、首音频延迟和主观听感验收；
- 根据线上 delta inter-arrival 数据进行自适应水位调整。

阿里云当前公开文档没有提供“保留 VAD 边界检测但关闭自动 response”的独立字段，所以本轮没有发送未经验证的兼容参数。Phase 2 应先做端点实验，再决定使用手动 commit 还是调整检索注入策略。

## 1. 问题定义

当前体验表现为：Agent 原本应该连续说完的一句话，被拆成多段输出，段与段之间有明显停顿。

听感相同的现象可能来自五个不同层次：

1. 用户的一次话语被 VAD 切成多个输入轮次，服务端为每段分别创建响应。
2. server VAD 已自动创建响应，应用的历史检索或安全分支又创建第二个响应。
3. 同一个响应的音频 delta 到达不均匀，本地 20 ms 播放时钟耗尽队列后插入静音。
4. 回声或环境噪声被识别成用户插话，当前响应被取消并清空播放队列。
5. 单个响应的原始 PCM 本身包含模型生成的较长韵律停顿。

因此不能只凭听感调大 buffer。必须先用 `response_id`、VAD 事件和本地队列指标确定停顿属于哪一类。

## 2. 当前链路

```text
浏览器麦克风
    -> WebRTC/AEC
    -> BrowserEdge._receive_audio
    -> Vision-Agents RealtimeInferenceFlow
    -> Qwen input_audio_buffer.append
    -> server_vad
    -> response.created
    -> response.audio.delta (24 kHz PCM)
    -> Vision-Agents AudioOutputStream (20 ms 分块)
    -> BrowserAudioTrack (重采样为 48 kHz)
    -> aiortc/WebRTC
    -> 浏览器扬声器
```

当前有两个独立的响应触发源：

- Qwen `server_vad` 在检测到话语结束后自动触发普通回答。
- `SessionAgentBridge` 在历史问题或安全事件完成处理后，通过 `inject_text()` 显式发送 `response.create`。

只要两个触发源同时参与同一用户轮次，就无法保证一次输入只对应一次输出。

## 3. 代码证据与根因分析

### 3.1 VAD 参数容易切断自然停顿

`agent_local_agent.py` 当前参数为：

```python
vad_threshold=0.35
vad_silence_duration_ms=int(os.getenv("QWEN_VAD_SILENCE_MS", "600"))
```

Vision-Agents 0.6.9 的 Qwen 适配器默认静音时长是 `900 ms`。当前改为 `600 ms` 后，用户在中文短句中一次正常换气、思考或动作发力造成的停顿，都更容易被判为话语结束。

`vad_threshold=0.35` 也高于 SDK 默认值 `0.1`。阈值过高时，较轻的尾音可能跌出语音区间，使同一句话被切成多段。阈值的具体含义和有效范围应以当前 Qwen 端点实测为准，不能直接照搬其他 Realtime API 的参数经验。

典型事件序列：

```text
speech_started
speech_stopped
response.created R1
response.done R1
speech_started
speech_stopped
response.created R2
response.done R2
```

如果一次预期话语出现两个 `speech_stopped` 和两个 `response_id`，问题发生在输入切段，不是播放器。

### 3.2 server VAD 与应用注入产生双重响应

`SessionAgentBridge.on_user_transcript()` 会在以下话语上启动额外任务：

- 包含“上次、历史、多少次、有效、训练总结”等词的历史问题；
- 包含“疼、痛、不舒服、受伤、不适”等词的安全事件。

任务最终调用：

```python
await self.qwen.inject_text(prompt, interrupt=True, feedback_id=feedback_id)
```

而 `inject_text()` 会执行：

```text
取消当前响应（如果存在）
conversation.item.create(input_text)
response.create
```

与此同时，server VAD 可能已经基于原始语音自动创建自然回答。最终事件可能是：

```text
speech_stopped
response.created R1          # server VAD 自动创建
audio.delta R1               # 用户先听到前半段
transcription.completed
本地检索完成
response.cancel R1
response.created R2          # 应用 inject_text 创建
audio.delta R2               # 停顿后开始第二段
```

这是“一句话像被 Agent 回答了好几次”的直接来源。增加 jitter buffer 只能掩盖 R1 内部的小抖动，无法把 R1 和 R2 合并成一个响应。

### 3.3 云端 delta 抖动会被转化为有效静音

WebRTC 音轨必须按固定时钟持续提供帧。`BrowserAudioTrack.recv()` 每 20 ms 被调用一次；队列为空时只能返回一个静音帧。

Qwen 的 `response.audio.delta` 是突发到达的。如果网络、服务端合成或 Python 调度短暂晚于播放速度，本地队列会耗尽：

```text
response.created R1
audio.delta R1
audio.delta R1
本地队列耗尽 -> 输出静音帧
audio.delta R1
audio.delta R1
response.done R1
```

此时从协议上看只有一个 `response_id`，但听感仍有断句。浏览器无法恢复这段音频，因为 Python 已经把“缺数据”编码成合法的静音 RTP 音频。

### 3.4 原 jitter buffer 的状态缺陷（本轮已修复）

原工作区实现正在增加 `start_buffer_ms`、`resume_buffer_ms` 和 `max_buffer_ms`，方向正确，但状态条件需要修正。

目前每写入一个 frame 就设置 `_has_audio=True`，而首次 `recv()` 使用：

```python
threshold = self._resume_frames if self._has_audio else self._start_frames
```

因此一旦有任何音频写入，首次播放就使用恢复阈值，`start_buffer_ms` 实际上不会生效。本轮已改为依据显式 `PRIMING` 或 `REBUFFERING` 状态选择阈值。

短回答也必须处理：如果一个完整回答短于启动阈值，而 `final=True` 已经到达，播放器应立即播放已有数据。本轮已增加 final-aware 排空和对应测试。

原实现正常响应结束时 `_has_audio` 没有形成清晰的话语边界。当前实现会在 final 后队列排空时回到 `IDLE`，下一段从新的 `PRIMING` 开始；若下一段在旧队列排空前到达，则保持连续播放。

### 3.5 假插话会清空仍在播放的音频

`DuplexQwenRealtime` 收到任何 `input_audio_buffer.speech_started` 都会：

1. 发出 `interrupted=True` 的音频完成事件；
2. 清空本地输出队列；
3. 取消当前 Qwen response。

如果扬声器回声或环境噪声穿过 AEC 后仍触发 server VAD，Agent 会在一句话中途突然停止。浏览器报告 `echoCancellation=true` 只说明约束已启用，不代表当前房间、音量和设备组合下不会产生假插话。

典型事件序列：

```text
response.created R1
audio.delta R1
speech_started             # 实际是回声或噪声
audio_output_done(interrupted=True)
response.cancel R1
BrowserAudioTrack.flush()
```

### 3.6 模型自身的韵律停顿

如果只有一个 `response_id`，本地没有 underrun，也没有 interruption，但解码后的 PCM 本身包含长静音，那么停顿来自模型/voice 的合成韵律。

提示词已经要求“每次回复只说 1-2 句”，但示例中仍有较长句子、多个逗号和感叹号。实时动作反馈可进一步限制为单个短句；不过只有在排除轮次和播放问题后，才应把它归因于模型音色。

## 4. 推荐方案

### 4.1 第一原则：一个 Turn 只能有一个响应所有者

推荐增加应用级 `TurnCoordinator`，由它独占 `response.create` 权限：

```text
LISTENING
    -> ENDPOINTED
    -> ENRICHING       # 可选：历史检索/动作事实
    -> RESPONSE_REQUESTED
    -> PLAYING
    -> DONE

任意状态 + 新的确认用户语音
    -> CANCELLED
    -> flush 当前 response
    -> 新 generation 的 LISTENING
```

必须满足以下约束：

- server VAD 只负责检测边界，不直接拥有最终回答；或应用完全不做逐轮二次注入。
- 只有当前 turn generation 可以发送 `response.create`。
- 历史检索、安全提示和普通回答都经过同一个调度器。
- 新用户轮次使旧检索结果、旧 response 和旧 PCM 全部失效。
- `response.done` 表示模型生成完成，不等于扬声器已经播放完成。

有三种实现路径：

#### 路径 A：server VAD 检测边界，但关闭自动 response（推荐验证）

服务端产生 `speech_started`、`speech_stopped` 和转写结果，应用完成必要检索后只发送一次 `response.create`。

优点：保留服务端 VAD，改动小于自建语音端点检测；能保证检索先于回答。

限制：必须先用当前 Qwen 模型和中国区端点实测是否支持“VAD 自动提交但不自动创建 response”的配置。不要直接发送其他厂商协议中的 `create_response=false` 并假设兼容。

#### 路径 B：关闭 server VAD，应用手动 commit（控制力最强）

使用经过验证的本地 VAD 或显式按键控制边界，然后发送：

```text
input_audio_buffer.commit
等待/获取转写或完成本地意图处理
response.create
```

优点：输入轮次、检索和响应创建完全由应用控制。

代价：需要可靠的本地 VAD、噪声环境测试和额外的端点延迟管理。不能手写一个简单音量阈值作为生产 VAD。

#### 路径 C：保留 server VAD 自动回答，不做逐轮注入（短期降级）

把历史摘要在会话开始或组间预取到上下文，普通轮次完全交给 server VAD。历史问题不再在转写后创建第二个 response。

优点：最快消除双重响应。

限制：无法保证每个问题都检索最新事实，不适合作为最终的 Agent Loop 架构。

在路径 A 未经端点实测前，路径 C 是比“自动回答后再 cancel 和重答”更稳定的临时方案。

### 4.2 建立显式、final-aware 的播放状态机

建议将 `BrowserAudioTrack` 改成以下状态，而不是用 `_playing + _has_audio` 推断：

```text
IDLE
  收到首帧 -> PRIMING

PRIMING
  buffered >= start_buffer -> PLAYING
  收到 final 且 buffered > 0 -> PLAYING

PLAYING
  队列为空且未 final -> REBUFFERING
  队列为空且已 final -> IDLE

REBUFFERING
  buffered >= rebuffer_buffer -> PLAYING
  收到 final 且 buffered > 0 -> PLAYING

任意状态 + interrupt/flush -> IDLE，并递增 generation
```

推荐初始配置范围：

| 参数 | 建议起点 | 说明 |
|---|---:|---|
| `start_buffer_ms` | 120-160 ms | 增加有限首包延迟，吸收常见 delta 抖动 |
| `rebuffer_buffer_ms` | 160-240 ms | underrun 后积累更多数据，避免反复停播 |
| `max_buffer_ms` | 800-1000 ms | 限制排队延迟，超过时记录异常并考虑取消过期响应 |
| 音频帧 | 20 ms | 保持当前 WebRTC/AudioOutputStream 契约 |

恢复阈值通常不应强制小于启动阈值。发生一次 underrun 已经说明链路抖动较大，恢复时积累更多音频往往比快速恢复后再次停顿更稳定。

播放器还应接收或维护以下边界信息：

- 当前 `response_id` 或等价 generation；
- 首个音频 frame；
- 输入 `final` 已到达；
- 实际开始播放时间；
- 队列真正排空并完成播放的时间；
- interrupt/flush 原因。

如果不希望修改 Vision-Agents 的公共结构，可在项目内包装音频输出流，或用本地 generation 将 Qwen response 与 `BrowserAudioTrack` 队列关联。不要修改 `.venv` 中的依赖源码。

### 4.3 VAD 参数先回到保守基线，再做 A/B 测试

本轮已将默认配置调整为：

```dotenv
QWEN_VAD_THRESHOLD=0.20
QWEN_VAD_SILENCE_MS=900
```

然后测试以下组合，而不是只听一两句话：

| 组 | threshold | silence ms | 目的 |
|---|---:|---:|---|
| 当前组 | 0.35 | 600 | 复现基线 |
| A | 0.20 | 900 | 接近 SDK 默认切段速度 |
| B | 0.20 | 1200 | 容忍发力、换气和短思考 |
| C | 0.30 | 1000 | 较强噪声环境折中 |

不要一开始设置到 1500-2000 ms。过长静音阈值虽然减少误切段，却会明显增加用户说完到 Agent 开始回答的延迟。

`vad_threshold` 也应从环境变量进入运行配置，并在会话启动日志中记录数值，但不要记录密钥或原始音频。

### 4.4 对假插话做确认，但保留真正 barge-in

不建议通过“Agent 播放时关闭麦克风”解决，因为这会破坏全双工和用户打断能力。

推荐顺序：

1. 先在耳机环境测试。如果假插话消失，基本可定位为回声路径。
2. 使用 `chrome://webrtc-internals` 检查真实设备的 AEC、输入能量和远端输出关联。
3. 降低扬声器音量、固定输入设备，比较内置麦克风与外接设备。
4. 在应用中记录“播放期间的 speech_started”和随后是否产生有效用户转写。
5. 若误触发仍多，再增加约 80-150 ms 的近端语音确认或使用可靠本地 VAD；确认后立即 cancel 和 flush。

确认窗口必须计入打断延迟预算。不要等待完整转写后才停止播放，否则用户会觉得 Agent 在抢话。

### 4.5 仅在确认是模型 PCM 后处理韵律

实时动作反馈建议限制为：

- 一次只说一句；
- 中文约 8-20 个字；
- 只包含一个动作指令；
- 避免列表、冒号、括号和多重解释；
- 计数与纠正分开触发，不合成长句。

例如：

```text
推荐：膝盖向外，继续起身。
避免：这一次整体不错，但是你的膝盖稍微有点向内，所以接下来请注意把膝盖向外推，然后慢慢起身。
```

如果必须做静音压缩，只能对同一个 response 内、两个已确认有声区间之间的长静音进行有上限的缩短，并保留最少自然停顿。此方案容易产生爆音、吞字和不自然韵律，应排在最后。

## 5. 可观测性与诊断方法

### 5.1 必须记录的事件

为每个事件记录 monotonic 时间，默认不记录原始 PCM 和完整转写：

```text
session_id
turn_generation
event.type
event_id
response_id
audio_delta_bytes
audio_delta_duration_ms
queue_depth_ms_before/after
playout_state
underrun_count
silence_frames_emitted
flush_reason
```

额外记录六个时刻：

- `user_speech_started_at`
- `user_speech_stopped_at`
- `first_audio_delta_at`
- `first_audio_played_at`
- `response_done_at`
- `playout_drained_at`

### 5.2 根据事件直接分类

| 观测 | 判定 | 处理位置 |
|---|---|---|
| 一次用户话语对应多个 `speech_stopped` | VAD 输入切段 | threshold/silence/VAD |
| 一次话语对应多个 `response.created`，其中一个来自注入 | 双重响应所有者 | TurnCoordinator |
| 一个 `response_id` 内 `underrun_count > 0` | delta/调度抖动 | jitter buffer |
| 播放中出现 `speech_started -> interrupted -> flush` | 用户真打断或 AEC 假打断 | AEC/插话确认 |
| 单 response、无 underrun、无 interrupt，但 PCM 有长静音 | 模型韵律 | 提示词/voice/模型 |

### 5.3 不要混淆的完成语义

- `response.done`：模型不再生成该 response。
- `final=True`：上游不会再为当前话语写入 PCM，重采样器应排出尾部。
- `playout_drained`：当前话语的最后一个音频 frame 已由本地音轨消费。
- 浏览器真实播放完成：还会受到 WebRTC/browser jitter buffer 和设备延迟影响。

目前反馈表中的 `completed` 更接近生成完成，不应解释为用户已经听完。

## 6. 分阶段实施计划

### Phase 0：增加诊断，不改变行为

1. 在 `DuplexQwenRealtime._process_events()` 增加结构化事件计时和 `response_id` 日志。
2. 给 `BrowserAudioTrack` 增加 queue depth、首播、underrun、flush reason 指标。
3. 增加每个 turn 的 `response.created` 计数。
4. 增加合成的 burst/pause 音频测试，区分协议分段和本地 underrun。

完成标准：一次复现后可以仅凭日志归类到上述五类之一。

### Phase 1：低风险缓解（已完成）

1. 将 VAD 默认值从 `0.35/600 ms` 调回约 `0.20/900 ms`，并开放 threshold 配置。
2. 修复 jitter buffer 的首次阈值选择和显式播放状态。
3. 增加 `response.audio.done` 与 final-aware 短回答排空。
4. 将动作中的生成要求缩短为一个短句。

完成标准：普通对话不再因 300-600 ms 自然停顿稳定地产生多个响应；同一 response 的合成 burst 测试无反复 underrun。

### Phase 2：解决响应所有权

1. 增加 `TurnCoordinator` 和 turn generation。
2. 验证当前端点是否支持 server VAD 不自动创建 response。
3. 将普通回答、历史检索、安全提示统一到单次 `response.create`。
4. 删除“自然回答开始后再注入并重答”的正常路径。

完成标准：除重试和明确用户打断外，一个 turn 恰好产生一个 `response_id`。

### Phase 3：真实设备与自适应缓冲

1. 在耳机、笔记本扬声器、外接麦克风三类设备上测试。
2. 根据 delta inter-arrival jitter 动态调整 120-240 ms 的目标水位。
3. 对真实 barge-in 和假插话分别统计。
4. 必要时加入短确认窗口，但保持低打断延迟。

完成标准：连续运行 10 分钟不累积播放延迟，且用户打断仍然及时。

## 7. 自动化测试建议

### 7.1 VAD/Turn 协议测试

使用伪 Qwen 事件流覆盖：

- 一个 turn、一个 response；
- 一次输入被切成两个 VAD turn；
- server 自动 response 与应用注入竞争；
- 新 generation 使旧检索和旧 response 失效；
- cancelled response 的迟到 delta 不得进入播放队列。

### 7.2 播放状态机测试

覆盖：

- 首次数据低于启动阈值时保持 PRIMING；
- 达到启动阈值后连续播放；
- final 到达时，短于阈值的短回答也会播放并排空；
- underrun 后达到恢复阈值才继续；
- interrupt 清空队列、重采样尾部和旧 generation；
- PTS 始终单调，每帧保持 20 ms；
- max buffer 背压不会死锁关闭流程。

### 7.3 真实 Qwen 测试语料

准备固定中文语料，每条包含标注停顿：

```text
我想练深蹲，[停 300 ms] 今天做十个。
我上次做了多少个，[停 600 ms] 有效的有几个？
等一下，[停 900 ms] 我的膝盖有点不舒服。
```

每组 VAD 参数至少重复 20 次，统计切段率、响应数量和端到首音频延迟，不能只做主观试听。

## 8. 验收指标

建议以以下指标作为首版门槛：

| 指标 | 目标 |
|---|---:|
| 非打断场景每 turn 的 response 数 | 恰好 1 |
| 应用额外增加的首播缓冲 | 不超过 200 ms（P95） |
| 同一 response 的本地 underrun | 5 分钟连续对话中为 0，或每 response 小于 1% |
| 本地生成的连续静音缺口 | 不超过 40 ms（非模型原始静音） |
| 确认用户插话到 flush | 不超过 250 ms（P95） |
| 连续运行后的排队延迟 | 不随运行时间增长 |
| 迟到的已取消 response 音频 | 0 frame |

首音频端到端延迟需要先测当前基线，再确定模型与网络部分的目标；不能把云端生成延迟和本地 120-160 ms 预缓冲混为一个指标。

## 9. 推荐决策

按优先级执行：

1. 使用本轮新增的事件与 buffer 指标，确认每次停顿对应几个 `response_id`。
2. 在真实语音下比较 `900 ms` 与 `1200 ms`，不要只凭离线测试固定参数。
3. 用 `TurnCoordinator` 消除 server 自动回答与 `inject_text()` 的双重响应。
4. 根据真实 delta 间隔决定是否需要自适应 jitter buffer。
5. 最后才处理 voice 韵律或尝试静音压缩。

预期最可能的组合根因是：较激进的 VAD 参数造成输入切段，历史/安全轮次存在双重 response，同时云端 delta 抖动被固定 20 ms 播放时钟转化为静音。三者需要分别治理，单独增大缓存不能彻底解决。

## 10. 相关文档

- [浏览器全双工 AEC 设计](./FULL_DUPLEX_BROWSER_AEC.md)
- [Qwen Realtime 多模态输入能力验证](./QWEN_REALTIME_MULTIMODAL_VERIFICATION.md)
- [Agent Loop、记忆、MCP 与 RAG 设计](./COACH_AGENT_MEMORY_MCP_RAG_DESIGN.md)
- [故障排除指南](./TROUBLESHOOTING_CN.md)
- [阿里云 Qwen Realtime](https://help.aliyun.com/zh/model-studio/realtime)
- [阿里云 Realtime 客户端事件](https://help.aliyun.com/zh/model-studio/client-events)
- [阿里云 Realtime 服务端事件](https://help.aliyun.com/zh/model-studio/server-events)
