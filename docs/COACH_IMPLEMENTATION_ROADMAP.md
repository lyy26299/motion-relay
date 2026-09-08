# 教练 Agent 可执行实施路线

更新：2026-09-07。依据：[架构与可行性设计](./COACH_AGENT_MEMORY_MCP_RAG_DESIGN.md)、[项目审查](./PROJECT_REVIEW_2026_09_07.md)。

## 1. 最终目标与当前交付

目标不是给 Qwen 增加一段“请记住”的提示词，而是让应用拥有可重放、可查询、带来源的训练事实：YOLO 关键点 → 本地动作事实 → 短期状态 → 长期记录 → MCP 查询 / RAG → 有限步 Agent Loop → 反馈与后续动作关联。

先限定单用户档案、单人固定机位、深蹲。LLM 负责理解和表达，不拥有最终动作次数。新增持久化须先落实授权与删除；默认不保存原始音视频，不逐帧 embedding。

本轮交付 M0-M4 的第一个可运行垂直切片：结构化姿态快照、单人深蹲 FSM、短期工作记忆和 SQLite 事实账本。它还不是完整 Agent Loop。Qwen 不拥有权威计数；SessionController 已接入后台 `LedgerWriter`，停止/异常时会排空事实队列并结束 session。

## 2. 逐阶段实施与验收

状态含义：已实现需有代码和自动测试；设备/准确率验证单独列出，不以合成测试替代。

| 阶段 | 任务与代码落点 | 可验收输出 | 依赖 / 状态 |
| --- | --- | --- | --- |
| M0 基线复现 | 修复安装入口、冷启动说明；固定依赖；保留浏览器 AEC 回归 | 新环境按文档启动；缺密钥、无设备、停止可控；离线测试可重复 | 已实现；配置检查与离线 smoke 已通过 |
| M1 姿态事实入口 | `models.py`、`geometry.py`、`pose_adapter.py`；接入 SessionController 和桌面 | 一次推理同时产出画面、全部人体 17 点、来源、时间、左右膝髋投影角；低置信度/多人/过期不冒充有效值 | 已实现并有测试；真实摄像头精度仍待标注 |
| M2 主人体与深蹲内核 | `exercises/squat.py`、`runtime.py`；创建带时间的重放夹具 | 站立→下降→底部→上升→站立只产生一次完整动作；另存有效次数；丢点、多人、换人、半程不拼接成一次 | 已实现并接入 UI/SessionController；有 FSM/runtime 和接线测试；真实摄像头精度仍待标注 |
| M3 短期记忆与即时反馈 | `working_memory.py`、`runtime.py`；版本化实时快照和 watchdog | 30 秒姿态环、2 分钟/100 条事件；当前组累计不随窗口过期；断云仍可本地计数 | 已实现并有边界测试；FeedbackArbiter 待实施 |
| M4 长期事实账本 | `memory/store.py`、`memory/writer.py`；SQLite WAL、事务 outbox、档案与删除 | 重启查询、重复写幂等、数字 SQL 查询、删除隔离 | 已实现并接入 SessionController；有 store、writer 和接线测试 |
| M5 受控语音轮次 | `turn_coordinator.py`、`qwen_bridge.py`；复用 `qwen_duplex.py`、BrowserEdge | 最终转写→检索→回答；最多一个活动 response；插话取消旧工具结果和待播音频；计数/暂停输出不被云端阻塞 | 兼容性 spike 提前做，完整接入依赖 M3；待实施 |
| M6 MCP 与 RAG | `mcp_server.py` / `memory/retrieval.py`，审核知识集 | 受控工具发现/调用、用户 scope 绑定、数值查询、情节/知识检索；每个结果有证据 ID、版本、适用条件 | server、检索服务和离线契约测试已实现；真实 stdio client 往返、中文向量召回和桌面接线待实施 |
| M7 Agent Loop 闭环 | `agent_loop.py`，决策 schema，反馈前后统计与播放状态 | Observe→Retrieve→Decide→Act→Observe；最多 2 轮决策、每轮最多 2 工具；超时/插话后无过期执行；方案更改由用户确认 | 核心 loop 已实现并有契约测试；尚未接入 SessionController/Qwen 播放 |
| M8 实际质量与发布 | 真人标注重放、真实设备、30 分钟运行、安装与隐私回归 | 逐次 precision/recall、计数误差、拒判覆盖率、RAG 命中、首音延迟、内存曲线；通过门后再扩展动作 | 贯穿各阶段，最终验收待完成 |

关键顺序：M1 → M2 → M3 → M4 → M6 → M7；M5 的协议可行性验证提前做，避免后期才发现模型会在检索完成前自动回答。M0 的安装整理不应拖到发布当天。

### M2 的下一步任务拆分

1. 固定 COCO 点位语义、机位要求与丢点期限；保留全部检测，单人候选稳定后进入校准。多人或单人重新入镜触发重置，不能把 `detection_index=0` 当作跟踪 ID。
2. 采集/编写授权的站立、完整蹲起、半程、抖动、遮挡、换人序列；按用户与会话划分测试集。合成数据只验证程序逻辑。
3. 用时间尺度平滑、滞回、最短持续时间实现独立纯函数 FSM；活动范围个体校准，阈值标注规则版本，不解释成医疗安全阈值。
4. 产出不可变 `MotionSnapshot` 与幂等 `rep_completed`，含完整/有效次数、时长、观测质量、证据窗口 ID。
5. 桌面显示权威次数；从 Qwen 指令中移除自主计数权。注入或播报按后续输出仲裁控制，不让两条路径分别计数。
6. 重放断言：10 个完整动作=10 次，2 个不满足规则时有效次数=8；无新帧 watchdog 失效进行中动作；镜像、变帧率、重启不重复计数。

## 3. 关键架构约束与可行性

| 决策 | 实施理由 / 失败边界 |
| --- | --- |
| 项目自有 Pose Adapter，不编辑 `.venv` | 已安装 SDK 把关键点丢弃且只取 `data[0]`；独立转换全部检测，更新 SDK 时可契约回归 |
| 一次模型推理 + 一个工作线程 + 1 个待处理帧 | 保持 CPU/GPU 数据只在工作线程转换；慢推理丢旧帧，不阻塞共享视频分发、不堆积并行模型调用 |
| 投影角度而不是动作正确率 | 2D 点集受机位、遮挡影响；髋膝踝不能证明脚跟离地、疼痛或脊柱形态 |
| 结构化事实优先，向量召回为辅助 | 角度、次数、时长必须可精确计算；摘要与 embedding 都可延迟生成，不能覆盖事实 |
| MCP 是受控工具协议，不是存储层 | 工具暴露允许的查询/提案；数据库服务维护授权、幂等与删除；禁止任意 SQL/任意命令 |
| Realtime 轮次先验证再集成 | 当前 server VAD 会自动响应；完整“先检索后回答”需要实测受控轮次，必要时评估独立 STT→Loop→TTS |
| 逐阶段质量门 | 代码单测可证明数据流与状态机，不能证明真人计数准确、真实声学 AEC 或医疗安全 |

按 1 名熟悉 Python/asyncio 的工程师、有少量标注与教练审核支持估计，原设计的 31–47 工程日仍是整体粗估，不是本轮代码完成时间。M1 是第一段可运行基础；后续最不确定的是主人体稳定性、真人动作阈值、Qwen 受控轮次与中文检索质量。若缺少真人测试数据，不能将 M2/M8 标为准确率达标。

## 4. M1 数据契约与代码接口

入口：`StructuredPoseProcessor(session_id=..., snapshot_sink=...)`，实现 SDK `VideoProcessorPublisher`；SessionController 为一次训练建立新的 session ID。

- `PoseSnapshot`：`coach.pose.v1`、模型文件名、session ID、stream epoch、frame ID、输入尺寸、单调观察/完成时间、可选媒体 PTS 秒数、处理耗时、置信度阈值、人体数组、可观测状态、投影角度。
- `PersonPose`：帧内检测下标、COCO 顺序 17 点、框和检测置信度；坐标和框均按输入宽高归一化；无 GPU tensor。
- `Keypoint`：`x/y/confidence`；非有限或缺失值用 `None`，序列化为 `null`。几何在恢复像素比例后计算，越界、低置信度、零向量不能得到伪造的 0°。
- 单人时左右侧独立计算；多人仍保存所有点，但整体角度全部不可判定。`observable` 只表示四个投影角度可计算，不表示训练合格。
- 快照默认只进入容量为 1 的 UI 队列；不是 30 秒短期记忆，也不会自动送入 Qwen 文本上下文或数据库。
- `observed_at` 在适配器收到帧、排队前赋值，不是摄像头硬件采集时间。`processing_ms` 含转换/推理/绘图/特征，不含等待队列和显示。`processed_at-observed_at` 可观察输入后总延迟。
- `stream_epoch` 拒绝换轨后的旧结果；`frame_id` 单会话递增，丢帧会留下间隔。它们不是用户身份或跨进程时钟。
- UI 按观察时间每次更新检查 1.5 秒 TTL；无新帧也会清空角度。停止、错误和新会话清空状态；该 TTL 为初始显示策略，不是后续 FSM 的动作连续性阈值。
- 停止解绑自己的共享 forwarder handler；不抢占其他视频订阅。关闭等待正在运行的原生推理在线程外结束，不假装 asyncio 取消可以杀死 GPU 内核。

## 5. 当前验证与运行

当前验证结果：

```text
51 tests passed
scripts/check_local_setup.py: 5/5 checks passed
scripts/smoke_pose.py --device cpu: passed
scripts/smoke_browser_aec.py --pose --pose-device cpu: passed
```

复现命令：

```bash
uv sync --locked
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/check_local_setup.py
.venv/bin/python scripts/smoke_pose.py --device cpu
.venv/bin/python scripts/smoke_browser_aec.py --pose --pose-device cpu
```

完整桌面启动仍需要真实摄像头、浏览器权限和有效 DashScope API key：

```bash
./run.sh
```

当前 UI 已展示本地权威深蹲次数、有效次数、阶段、可见性/暂停状态和记忆 writer 状态。MCP server、检索服务和 Agent Loop 有离线实现与契约测试，但尚未由 `SessionController` 自动拉起；受控 Qwen 文本桥、真实 stdio client 和跨会话问答仍属于 M5-M7 后续接线，不能从离线测试推断为桌面能力已上线。

后续进入 M2 前还需做真人固定机位、侧面/斜侧面和遮挡样本评估。当前角度只供观测，不能据此提供确定性损伤风险判断。
