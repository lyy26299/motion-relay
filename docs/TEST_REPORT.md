# Architecture V2 — Test and Benchmark Report

更新日期：2026-09-24；历史实验日期保留。本报告只将实际运行的项目标为 PASS；微基准、协议测试和真实硬件体验分开。

> 版本说明：第1–12节保留 V2.0 固定实验；V2.1 实际结果、测量修复和剩余限制见第13节；V2.2 单调回执增量见第14节。

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

## 13. V2.1 — 响应身份、原生准入与基准隔离

### 13.1 固定版本和证据

增量起点 `46a2061fedebdd15c52cc4e8bd2441683ae01d14`；本轮代码/测试/基准受测快照 **`dc1ddd7fc797a760918a532888cc25d4de3c036d`**。后续仅文档提交不改这个固定测量归属。

[Push CI 35847571208](https://github.com/lyy26299/motion-relay/actions/runs/35847571208) 与 [PR CI 35847574380](https://github.com/lyy26299/motion-relay/actions/runs/35847574380) 均整体 success。以下数值来自 push 工件 [10744185511](https://github.com/lyy26299/motion-relay/actions/runs/35847571208/artifacts/10744185511)。下载 ZIP 的 SHA256 为 `f48a45ea90e689298dca23980315c0ff28403514bdaa3f2c5768f0874cedcb9b`；已核对 source.zip 中代码/测试/脚本与本地实现一致。

环境：Python3.13.15、uv0.12.18、Linux6.17.0-1022-azure x86_64/glibc2.39、SQLite3.45.1、vision-agents0.6.9。依赖仍由原 uv.lock 固定，没有本轮运行依赖或 schema 变更。sqlite_source_id 为 `2024-01-30 16:01:20 e876e51a0ed5c5b3126f52e532044363a014bc594cfefa87ffb5b82257ccalt1`；已记录来源，不代表供应商补丁状态已核实。

### 13.2 实际命令与结果

```bash
uv sync --locked --extra dev
uv run --no-sync python -m unittest discover -s tests -v
uv run --no-sync python -m compileall -q coach scripts agent_local.py agent_local_agent.py
uv run --no-sync ruff check . --select E9,F63,F7,F82
```

最终 **166/166 passed，0 failures，0 errors，0 skipped**；unittest **1.887s**，进程 wall time **12.22s**。locked sync、compileall、关键 Ruff 规则和两臂 benchmark 均通过。原124项保留，其中一个成功 response.done fixture 增加明确 `status=completed`，原断言未删除或放宽；缺失状态另测为 generation_unknown。

| 新增文件 | 数量 | 覆盖 |
|---|---:|---|
| tests/test_voice_response_state.py | 9 | 单活动响应、重复/竞争创建、缺失/外来ID、终结重放、有界退役、非法输入 |
| tests/test_native_output_admission.py | 9 | 原生共享仲裁、安全抢占、VAD、普通/路由转录、关闭、无数据库准入 |
| tests/test_voice_output_boundaries.py | 20 | 固定SDK下音频/字幕/created/item/done、注入等待、发送不确定、取消超时、旧reader、关闭发送等交错 |
| tests/test_benchmark_source_isolation.py | 4 | 文件路径与哈希、外部回退拒绝、无来源拒绝、符号链接逃逸拒绝 |
| **合计** | **42** | **38项语音边界 + 4项测量隔离；124→166** |

### 13.3 失败尝试、修复及环境限制

第一次完整 run35846213087（2f08e8d）运行162项，161通过、1 error、0 failures，耗时1.908s。错误仅在新关闭测试：真实SDK会把 `_real_client` 清空，测试却关闭后从该字段访问fake。5939a225保留关闭前的client引用，继续断言只发送一次、且新增确认client.close被调用；未更改运行逻辑或削弱预期。

修正后 run35846599333（5939a225）162项全通过，2.010s /13.95s wall time。但检查 benchmark-main.json 发现基线不存在的 voice_state 被环境可编辑安装回退加载。**这个 run 的测试结果有效，其性能输出不用于本轮最终对比。** dc1ddd7 增加来源验证、显式可选模块存在检查和4项测试，并重跑得到本节最终166项与新基准。详细原因见 [BENCHMARK_SOURCE_ISOLATION](BENCHMARK_SOURCE_ISOLATION.md)。

本地工作容器 Python3.13.5 /uv0.10.0 的 `uv sync --locked --extra dev` 遇DNS失败；原始本地discover有6个缺失依赖导入错误，不归类为运行代码缺陷。纯身份/准入18项与源码隔离4项在本地通过。SDK形状stub的27项辅助逻辑检查不作为真实SDK证据；真正兼容性证据来自完整依赖CI。

### 13.4 有来源证明的微基准

同一个 job、同一脚本、同一环境，对 main=f2f104d3 与 current=dc1ddd7 分别导入。预热100次、预建pose/response ID、nearest-rank分位数。普通指标N=5000，查询N=1000，Agent abstain N=500。新JSON含 source_modules：main实际13个coach模块、current16个；每项保存相对路径与SHA256，外部源码导入会使基准失败。current模块哈希已与source.zip核对。

原始测量：[main isolated](benchmarks/2026-09-23-v2.1-main-isolated.json)、[V2.1 dc1ddd7](benchmarks/2026-09-23-v2.1-dc1ddd7.json)。此前V2.0固定JSON继续保留，绝不覆盖成不同版本的数据。

全部时间为 **µs**：

| Metric | Main P50 | Main P95 | Main P99 | V2.1 P50 | V2.1 P95 | V2.1 P99 |
|---|---:|---:|---:|---:|---:|---:|
| FSM update | 6.621 | 12.503 | 12.789 | 6.601 | 12.679 | 13.020 |
| MotionRuntime ingest | 8.712 | 15.006 | 17.415 | 17.838 | 33.919 | 39.613 |
| WorkingMemory add_pose | 0.590 | 0.702 | 0.786 | 0.589 | 0.708 | 0.869 |
| WorkingMemory view | 3.781 | 3.884 | 3.943 | 3.771 | 3.883 | 3.962 |
| bounded queue roundtrip | 2.473 | 2.761 | 4.804 | 2.404 | 3.994 | 5.250 |
| memory query：5个空session | 120.948 | 129.960 | 136.997 | 121.336 | 153.273 | 252.340 |
| Agent abstain scheduling | 173.666 | 186.763 | 212.261 | 178.864 | 190.886 | 206.662 |
| feedback arbitration | 未实现 | 未实现 | 未实现 | 3.314 | 3.457 | 3.868 |
| response identity cycle | 未实现 | 未实现 | 未实现 | 2.296 | 2.494 | 2.608 |

身份门cycle为 begin→accepts(audio)→finish_audio→finish；不是单个判断，更不是完整Qwen/PCM/网络路径。Main没有该模块，不计算相对提速。ingest、查询、队列部分尾延迟高于main，本轮没有证明系统整体更快。由于本轮没有改变FSM/存储算法，也不能把跨run波动归因于语音修复；代码审查与性能因果结论分开。

### 13.5 未运行和剩余风险

NOT RUN：真实云Qwen与当前在线协议兼容性、摄像头/MPS/麦克风扬声器AEC、端到端播放ACK及打断时延、完整Mermaid渲染、全量style lint/typecheck/coverage、多小时故障压测、用户实验。有限事件交错测试不是所有调度顺序的证明。

已解决的是默认入口共享准入、已知响应ID竞争和不确定状态保守阻断。ordered injection candidate仍是unverified，原生内容未经过AgentLoop事实验证，退役窗口仅256项，原生lease12秒并非speech-duration调度，取消等待0.2秒不等于远端/扬声器已停止。并行feedback持久化更新的单调状态归并、完整关闭/重连和全进程资源上界仍需后续工作。


## 14. V2.2 单调反馈回执增量（2026-09-24）

### 14.1 固定起点与实际运行环境

本次继续已有 Draft PR #1，不重新创建空分支。开始 main 仍为 `f2f104d3c280972bc9e3aecb75fdafe8c76a4f85`；V2.1 起点为 `fb4c3b1b104accc144fdb547b878cfa800682fe4`。从其 CI run35848471670 下载源码工件，核对全部99个源码文件的 Git blob SHA，与清单零差异后再修改。

实现提交 `1741b4574aa12ec3b7eb138cc729d9766c01c020`；本节固定测试/基准提交 `b67a03c204fbd121825983a021bf55a1dccdce6d`。之后文档与实测JSON提交不修改实现，不能把本节数据重新标成另一个测量快照。

| 环境 | 实际结果 / 限制 |
|---|---|
| 本地 Python | 3.13.5；网络/DNS不可用，非完整项目依赖环境 |
| 本地 baseline locked sync | `uv sync --locked --extra dev` 失败于依赖下载；不是代码失败 |
| 本地 baseline discovery | 108项，101通过，7个模块导入错误；缺少getstream/媒体SDK依赖 |
| 本地新增定向测试 | 33通过，0失败/错误/跳过，0.129秒 |
| 本地最终 discovery | 141项，134通过，同样7个导入错误；0.840秒；不冒充全量SDK验证 |
| 完整 CI | Python3.13.15、uv0.12.18、SQLite3.45.1、Linux x86_64 |
| 锁定依赖 | vision-agents0.6.9、mcp1.29.1、aiortc1.14.0、av16.1.0；锁文件未改 |
| CI lint | Ruff0.16.6，E9/F63/F7/F82 |

修改前已在原始源码复现 `generated→interrupted→迟到queued` 将 status/playback_state 改回 queued/unknown。低层兼容API未改，因此新benchmark仍能保留缺陷对照；默认Bridge改为调用新能力。

### 14.2 完整 CI 结果与证据

[固定 push CI 35973634675](https://github.com/lyy26299/motion-relay/actions/runs/35973634675)，job107548863336：**199 passed，0 failures，0 errors，0 skipped**；unittest2.084秒，进程wall time12.95秒。锁定依赖同步、compileall、关键Ruff、git diff检查、通用main/current基准与回执V2.1/current基准全部通过。[对应PR CI 35973639079](https://github.com/lyy26299/motion-relay/actions/runs/35973639079) 的所有步骤也成功。

[工件10796629200](https://github.com/lyy26299/motion-relay/actions/runs/35973634675/artifacts/10796629200) 的ZIP SHA256为 `e9b6313f20131456db3bd84383d3b5cf71b6617b383ec22a8392f9972fc5b8c7`。实际下载核对：源码与本地实现相同，仅有尚待提交的README/架构文档文字不同；新代码、旧代码和测试均无差异。远端源码树 `b256da61abccc2606989ca7c3f7417370ab33f63` 与本地已验证代码树也完全一致。

新增33项是6个状态代数、18个存储、2个双连接竞争、7个异步Bridge测试。原有166项测试未删除、未修改。10态全部100个有序对、1000个有序三元组及24种观察排列包含在上述测试方法中，不额外虚增测试数量。

两个回执benchmark的source manifest分别有6个V2.1模块、8个V2.2模块，已逐一与对应source.zip内容SHA256核对；当前通用benchmark16模块也匹配。V2.1正确将新guarded模块报告为不可用，没有editable安装回退。

### 14.3 回执 invariant 与成本

同一job、同一harness、100预热+1000计时样本；单位均为微秒。cycle执行三次观察：generated→interrupted→queued；回执在计时前创建。

| 路径 | P50 | P95 | P99 | 终态回退次数（含预热，共1100） |
|---|---:|---:|---:|---:|
| V2.1 legacy三观察 |115.788|143.722|202.362|1100|
| V2.2保留的legacy三观察 |115.388|143.991|168.919|1100|
| V2.2 guarded三观察 |325.485|358.808|384.806|0|
| V2.2纯状态join |0.321|0.351|0.380|不适用|

原始数据：[V2.1回执JSON](benchmarks/2026-09-24-feedback-v21.json)、[V2.2回执JSON](benchmarks/2026-09-24-feedback-v22.json)。legacy与guarded语义不同，不能称公平速度竞赛。guarded增加scope与事务检查，成本更高；本轮收益是修复可重复的不变量违例，不是提速。循环样本是固定对抗序列，不代表真实生产错误率。

### 14.4 通用架构回归微基准

[main JSON](benchmarks/2026-09-24-benchmark-main.json)、[V2.2 JSON](benchmarks/2026-09-24-benchmark-v2.json)，同一固定CI。大多数n=5000；检索n=1000；Agent n=500；各100预热。

| 指标（µs） | main P95 | V2.2 P50 | V2.2 P95 | V2.2 P99 |
|---|---:|---:|---:|---:|
| FSM update |14.678|7.434|13.997|14.908|
| MotionRuntime ingest |18.556|20.880|39.565|54.192|
| WorkingMemory view |4.148|4.048|4.117|4.188|
| bounded queue往返 |2.805|2.674|2.755|2.816|
| memory query，5空session |164.952|140.315|165.843|173.658|
| Agent abstain scheduling |345.473|216.088|243.170|267.996|
| Feedback arbitration |N/A|3.647|3.787|4.589|
| Response identity cycle |N/A|2.655|2.776|2.975|

main→V2.2包含先前V2.0/V2.1改动，不能把差异归因为本次recorder。单次合成测量也不足以宣称普遍加速或回归；尤其ingest高于main，不能包装为全系统优化提速。

### 14.5 已测与未测边界

已测：状态收敛、旧回调不能回退终态、事务异常回滚、双连接竞争、scope删除/同名重建、response ID冲突、可信播放fixture保留、取消等候后的工作线程、容量/超时/错误分类、全量既有测试与本地函数开销。

NOT RUN：真实Qwen/API、摄像头/麦克风/扬声器、真实播放ACK/AEC/听感、设备端到端延迟、磁盘压力/断电/满盘、所有跨进程/多会话交错、用户表现与坚持度、SQLite供应商补丁核验。已有WebRTC本地回环通过，不等于真实声学验收。

关键剩余风险：observer/IO容量拒绝仍可能遗失回执；无持久化观察日志/outbox；timeout不证明没提交；legacy API仍可绕过；已绑定ID不证明请求因果；原生内容未验证；没有真正played_at；所有输出的删除即时撤销和全局shutdown上界未完成。

对应架构、设计比较、外部来源与迁移见 [FEEDBACK_RECEIPTS_V2_2](FEEDBACK_RECEIPTS_V2_2.md)；主架构文档第38节也已同步。保持Draft，不自动合并。
