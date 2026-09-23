# Architecture V2 — Test and Benchmark Report

日期：2026-09-23。本报告只将实际运行的项目标为 PASS；微基准、协议测试和真实硬件体验分开。

## 1. Source / Environment

| 项目 | 实际值 |
|---|---|
| main baseline | `f2f104d3c280972bc9e3aecb75fdafe8c76a4f85` |
| baseline CI snapshot | `82625e39386632e723d6aed91e2424de859126c6`，仅增加测试工作流，应用代码等于main |
| validated implementation | `d45ddeb76badc4f6d5ef77599cdfe9d6865d52d2` |
| branch | `research/agent-architecture-v2` |
| OS / architecture | Ubuntu24.04.5，x86_64；Linux6.17.0-1022-azure，glibc2.39 |
| Python | 3.13.15 |
| uv | baseline0.12.17；implementation0.12.18 |
| SQLite runtime | 3.45.1，供应商WAL修复状态未核实 |
| runtime dependencies | vision-agents0.6.9、mcp1.29.1、aiohttp3.14.3、aiortc1.14.0、av16.1.0、websockets15.0.1 |
| other recorded packages | numpy2.2.6、torch2.14.0、ultralytics8.4.142、pydantic2.13.5 |
| lint | ruff0.16.6；仅关键错误规则E9/F63/F7/F82 |

运行依赖与 `uv.lock` 未修改。验证时用 `uv sync --locked --extra dev`，完整安装清单保存在CI工件。宿主Python和SQLite实际版本必须另行检查，不能根据上述CI环境推断用户Mac配置。

## 2. Evidence Index

[基线CI run 35758989345](https://github.com/lyy26299/motion-relay/actions/runs/35758989345)，job106851942788，结果工件10710170340。

[实现验证CI run 35815372116](https://github.com/lyy26299/motion-relay/actions/runs/35815372116)，job107035663243，[完整证据工件10731770995](https://github.com/lyy26299/motion-relay/actions/runs/35815372116/artifacts/10731770995)。包括source.zip、commit.txt、commits.txt、tests.log、test-time.txt、lint.log、sync.log、installed.txt、python/uv/platform、两个benchmark JSON与补丁校验和。

验证补丁SHA256：`ab4f0f5ac1bb59db876e66503af0cad8d4384c261edc738d799c1c04742d12db`。CI在应用补丁前执行完整checksum与`git apply --check`。下载后的source.zip含77个文件，与本地待提交版本逐文件比对：0缺失、0差异。

**交付状态与测试状态分开**：run35815372116的测试、语法、lint和benchmark步骤全部成功，但最后的git push被Actions token的workflow写权限拒绝，故该run整体结论为failure。随后通过已授权GitHub连接以`force=false`快进到完全相同的已验证commit d45ddeb，未修改main，也未重新生成未经测试的代码。临时补丁传输文件和写权限工作流已从最终源码树移除。后续普通CI使用read-only contents权限。

长期可读的测量副本：[baseline JSON](benchmarks/2026-09-23-main.json)、[v2 JSON](benchmarks/2026-09-23-v2.json)。Artifacts有保留期限，Git中的JSON保留测量版本与限制。

## 3. Commands Actually Run

```bash
uv sync --locked --extra dev
uv run --no-sync python -m unittest discover -s tests -v
uv run --no-sync python -m compileall -q coach scripts agent_local.py agent_local_agent.py
uv run --no-sync ruff check . --select E9,F63,F7,F82
```

基线为`uv sync --locked`后执行同一unittest discover命令。工作流显式使用Bash pipefail，测试错误不会被`tee`退出码掩盖。

## 4. Baseline / Final Result

| 测试组 | Total | Passed | Failed | Errors | Skipped | unittest计时 | 进程wall time |
|---|---:|---:|---:|---:|---:|---:|---:|
| 基线全部现有测试 | 91 | 91 | 0 | 0 | 0 | 1.241s | 10.66s |
| V2全部确定性测试 | 124 | 124 | 0 | 0 | 0 | 1.665s | 14.09s |

unittest计时不包含全部导入/进程启动；wall time包含这些开销。两个时间不能混用为端到端Agent延迟，也不能据此声称吞吐改善。

原91项测试没有删除或放宽。新增33项。compileall PASS。关键ruff规则PASS。全量style lint、类型检查、覆盖率百分比：**NOT RUN**，不将关键lint通过描述为全量代码质量检查通过。

工作容器缺少vision_agents/av/aiortc等依赖，最初本地discover有5个导入环境错误；这些不被归类为原代码bug。依赖完整的远端基线/最终suite均实际运行通过。

## 5. New Tests

| 文件 | 数量 | 覆盖 |
|---|---:|---|
| `tests/test_runtime_boundaries.py` | 9 | 应用暂停持续来帧、跨stream半次动作、旧epoch、rep/dialogue时间零点、嵌套事实隔离、writer close/admission、sticky overflow、UI满队列stop |
| `tests/test_agent_architecture_v2.py` | 17 | 仲裁8项、operation pool3项、bridge与集成6项；包含优先级、TTL、cooldown、重复、旧lease、不合作取消、线程容量、慢存储、scope和burst |
| `tests/test_qwen_architecture_v2.py` | 7 | 注入失效、取消期间失效、迟到done、generation不冒充playback、stale音频、VAD先失效、observer容量 |

原suite继续覆盖几何、FSM/invalid reps、可见性、pose epoch、UI过滤、Agent allowlist/budget/evidence/scope、memory事务/幂等/删除、MCP契约、Qwen旧契约和本地浏览器媒体传输。

## 6. Regression Reproduction

把新增领域测试中的前8项放入未修改main源码目录，用本地Python3.13.5执行：8项中7项失败、1项通过。这是**故意的负向复现**，不是最终代码仍有7项失败。

| 复现项 | 基线行为 | 修改后 |
|---|---|---|
| 显式pause后连续standing/frame | paused变false | PASS，计数不推进 |
| new epoch结束旧partial rep | 产生跨流RepRecord | PASS，无拼接 |
| 修改view嵌套facts | 后续view被污染 | PASS，只读隔离 |
| rep起点0 | duration错误 | PASS，0被保留 |
| dialogue时间0 | 被当前时间替换 | PASS |
| writer close与submit竞争 | close期间仍可接纳 | PASS，admission barrier |
| writer overflow后状态 | 后续saved覆盖完整性失败 | PASS，sticky failure |
| 旧epoch帧拒绝 | 原本已通过 | 保留PASS，不冒充新能力 |

UI控制队列测试依赖新helper，没有当作基线已有API强行运行。

## 7. Async / Race Coverage

已运行：旧工作在新turn/取消后返回；不合作异步操作；同步任务调用者取消后仍占容量；慢DB完成后禁止注入；scope epoch变动；安全暂停不等SQLite；低优先级被safety抢占；旧lease/done不能清除新输出；VAD先失效再进行慢取消；observer/bridge burst容量限制；关闭时有限等待与异常回收。

上述为有控制的交错测试，不是所有调度顺序的形式化证明。迟到response.created的完整请求归属、实际TTS/扬声器播放回执、网络黑洞长压测、跨进程删除广播、重复创建会话的全局executor容量：**NOT RUN / 尚未完整实现**。

## 8. Integration and External Services

新的integration实际走：PoseSnapshot → MotionRuntime → SquatFSM → event → WorkingMemory → AgentTrigger → AgentLoop → FeedbackArbiter → FakeVoice。没有跳过决策验证来直接调用mock输出。

固定vision-agents0.6.9环境中的Qwen事件测试已运行，使用offline fake key/事件，不发送真实云请求。已有真实本地WebRTC loopback测试也运行，因此这里的“离线”表示无外部服务，不表示完全不使用本地socket。

真实摄像头、Mac MPS推理、麦克风/扬声器AEC、DashScope网络、真实LLM/TTS延迟、外部MCP服务、用户实验：**NOT RUN**。不宣称模型服务当前可用性、动作识别准确率、可听见打断时间或运动效果已验证。

## 9. Benchmark Method

同一个CI job、同一Python和宿主，使用V2提供的同一harness，分别导入main与V2源码。100次预热，预先构造PoseSnapshot，`perf_counter_ns`，nearest-rank P50/P95/P99。大部分N=5000；memory N=1000，Agent abstain N=500。

```bash
mkdir -p /tmp/motion-baseline
git archive f2f104d3c280972bc9e3aecb75fdafe8c76a4f85 | tar -x -C /tmp/motion-baseline
uv run --no-sync python scripts/benchmark_architecture.py \
  --source-root /tmp/motion-baseline \
  --source-revision f2f104d3c280972bc9e3aecb75fdafe8c76a4f85 \
  --iterations 5000 --output artifacts/benchmark-main.json
uv run --no-sync python scripts/benchmark_architecture.py \
  --source-root "$PWD" --source-revision "$(git rev-parse HEAD)" \
  --iterations 5000 --output artifacts/benchmark-v2.json
```

## 10. Measured Results

全部时间单位为 **微秒µs**，不是毫秒。

| Metric | main P50 | main P95 | main P99 | V2 P50 | V2 P95 | V2 P99 |
|---|---:|---:|---:|---:|---:|---:|
| clock/call overhead | 0.091 | 0.101 | 0.120 | 0.091 | 0.101 | 0.120 |
| FSM update | 7.614 | 14.587 | 18.405 | 7.423 | 14.136 | 17.502 |
| MotionRuntime ingest | 10.500 | 18.375 | 31.930 | 20.348 | 38.432 | 52.228 |
| WorkingMemory add_pose | 0.671 | 0.781 | 0.921 | 0.671 | 0.782 | 0.912 |
| WorkingMemory view | 4.117 | 4.208 | 4.539 | 4.167 | 4.268 | 4.910 |
| bounded queue roundtrip | 2.725 | 2.806 | 3.116 | 2.625 | 2.705 | 2.765 |
| query_training：5个空session | 137.988 | 169.157 | 214.412 | 137.658 | 162.435 | 171.331 |
| Agent abstain scheduling | 216.596 | 295.083 | 328.275 | 218.178 | 256.330 | 307.265 |
| Feedback arbitration | 未实现 | 未实现 | 未实现 | 3.667 | 3.807 | 4.629 |

MotionRuntime按测量平均耗时倒数计算的调用速率：main83,395 calls/s，V2 45,393 calls/s。这不是包含推理/网络/磁盘的摄像头帧吞吐。

### Interpretation

**没有全面性能提升。** ingest P95约2.09倍，增加约20.057µs。本轮选择了事件事实所有权和流/暂停正确性，接受额外CPU代价；需要真实设备持续负载再判断是否满足产品预算。

WorkingMemory view没有采用每次深拷贝整个事件窗口，而在写入时freeze；P95为4.268µs，避免反复复制。Agent和查询尾延迟的少量变化来自一次合成运行，不足以证明统计显著提升；尤其存储query实现没有因此获得已证明的优化。

memory fixture使用内存SQLite，只有5个session且无rep。它只验证harness/接口开销，不是生产历史检索benchmark。queue roundtrip不是满载事件排队时延。仲裁基线为空，所以不能计算相对提速百分比。

没有置信区间、跨多台机器重复实验、模型网络/TTFT/TTS/扬声器分位数或用户效果数据。

## 11. Architecture Regression Summary

| 维度 | 证据与变化 |
|---|---|
| Test suite | 91→124，旧测试保留 |
| Module responsibility | 增加operations/arbiter/immutable三个小边界；没有重写AgentLoop框架 |
| Latency-sensitive IO | bridge同步业务IO移出loop；本地暂停不等SQLite |
| Queue bounds | bridge4+latest1，IO4，arbiter1/0，UI16；原pose/writer/audio上界保留 |
| Cancellation | 结果失效和实际工作停止分离；遗留工作仍计入容量 |
| Stale handling | 执行前guard与已知response ID过滤；created/原生旁路仍有风险 |
| Memory isolation | scoped MCP/epoch保留，事件facts冻结，writer失败不可洗白 |
| Observability | 增加边界日志/计数器/benchmark；完整tracing未实现 |
| Known regression/trade-off | ingestCPU成本上升；部分低优先级反馈被拒绝；long speech可能因TTL被截断 |

## 12. Remaining Risks and Release Gates

必须进一步验证统一语音出口、provider请求ID与播放ACK、exercise能力绑定、SQLite引擎补丁、真实媒体/网络/磁盘负载和全链路trace。当前124项PASS不覆盖这些缺口，详细清单见审计A13–A19。

常规CI会对后续文档/CI提交再运行相同suite并保存新HEAD证据；本报告的固定测量表始终对应d45ddeb，不用后续新HEAD名称冒充原测量版本。
