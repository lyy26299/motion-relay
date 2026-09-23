# Benchmark 源码来源隔离

日期：2026-09-23；V2.1 增量中的测量修复。此问题不修改 Agent 运行逻辑。

## 发现与证据

CI run 35846599333（5939a225）的测试162项全部通过，但下载后的 benchmark-main.json 却出现 voice_response_identity_cycle 数值。原始 main 并无 coach/voice_state.py，因此不能把这项数值当作基线能力或据此计算提速。

只在 sys.path 最前加入基线目录并不充分：可编辑安装的模块查找器可能在基线缺少子模块时回退到当前工作区。既有模块能从基线加载，新可选模块却来自当前版本，形成混合来源。该 run 的测试结果保留为有效；两份性能输出不用于本轮最终比较，修复后重新执行。

## 修改

导入可选 voice_state 之前先确认它存在于指定 source-root；不存在时返回 available=false。计时结束后遍历已加载 coach/coach.* 模块，解析真实路径并验证全部属于指定源码根；无路径、外部路径或通过符号链接逃逸都抛出明确错误，不输出成功报告。

每份新 benchmark JSON 增加 source_modules，保存实际模块相对路径及 SHA256。哈希检查在计时结束后进行，不计入采样。它提供来源可审计性，不证明任务环境、硬件和统计方法本身无偏。

## 测试

新增 tests/test_benchmark_source_isolation.py，4项：正确路径清单与哈希、外部可编辑回退拒绝、缺失来源拒绝、符号链接真实路径拒绝。本地 Python3.13.5 已运行4项通过。完整 CI、最新166项 suite 结果和重新测量值以 TEST_REPORT.md 的 V2.1 增补为准。

## 使用与限制

继续使用 scripts/benchmark_architecture.py --source-root。不要手工把错误来源标记为当前 SHA。原V2.0固定JSON保持历史版本，不用本轮新结果覆盖；本轮最终JSON须保留实际受测commit、模块哈希、环境和样本量。

微基准仍不包含摄像头、推理、模型网络、TTS与实际扬声器播放。源码来源正确不等于生产时延已验证，也不把不同run的小幅波动当成性能改进。
