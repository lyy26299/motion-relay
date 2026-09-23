# Architecture V2 迁移与回滚

日期：2026-09-23。只在 `research/agent-architecture-v2` 审查，不自动合并 main。

## 1. Before / After

| 接口/行为 | Before | After |
|---|---|---|
| Runtime.pause | 后续standing可能让FSM计数恢复 | application pause锁存，显式resume才解除 |
| 新stream epoch | 可能拼接旧partial rep | 丢弃旧流半次动作 |
| WorkingMemory event facts | 共享嵌套dict可误改 | 写入时冻结，读取应视为只读 |
| Agent取消/超时 | 可能等待不合作工作 | 决策结果先失效；遗留工作继续占容量 |
| Bridge数据库访问 | 同步调用可能阻塞loop | 有界owned operation离开loop |
| Feedback | 回调先后顺序隐式竞争 | 单活动lease，过期/低优先级显式拒绝 |
| Qwen完成回调 | 与播放完成混合风险 | generation_completed，playback仍unknown |
| Ledger overflow | 后续成功可覆盖失败 | sticky failure，不再宣称完整保存 |
| UI控制 | 无界排队 | 16项，stop/close清理旧控制 |

## 2. Compatibility

没有修改 SQLite schema、既有 MCP 工具名、主要启动入口或 `pyproject.toml/uv.lock`。原91项测试全部保留。新代码依旧要求项目声明的 Python `>=3.13,<3.14` 与固定 vision-agents0.6.9。

`Qwen.inject_text` 新增可选 `is_current`、`playback_guard` 参数，旧无guard调用保持兼容；只有受控调用具备新执行有效性保证。自定义fake/provider要接收并尊重这两个回调，不能只接受参数却忽略语义。

## 3. 有意的行为变化

不要通过摄像头站立帧解除用户显式暂停。应用控制应调用 `motion_runtime.resume()`；恢复后仍由FSM重新建立可靠动作起点。不能让LLM直接写rep count。

不要修改 `memory.view().events[i].facts`。需要编辑时创建业务副本，重新经过合法写入口；嵌套序列现在可能是tuple。不要绕过只读契约修改底层dict。

反馈消费端必须将 `generation_completed` 与真正 `played` 分开。当前没有完整浏览器播放ACK，因此不能凭历史 `completed` 推断用户已听完；不自动批量回填 played_at。

满载时可能明确拒绝低优先级语音、异步observer或新操作。这是避免过期积压的设计变化；请使用计数器识别负载，不把拒绝隐藏成成功。

## 4. 获取和验证分支

先保存本地未提交工作，避免覆盖。以下命令不会修改远端main：

```bash
git fetch origin
git switch --track origin/research/agent-architecture-v2
# 已有本地分支时改用：git switch research/agent-architecture-v2
uv sync --locked --extra dev
uv run --no-sync python -m unittest discover -s tests -v
uv run --no-sync ruff check . --select E9,F63,F7,F82
uv run --no-sync python -m compileall -q coach scripts agent_local.py agent_local_agent.py
uv run --no-sync python scripts/benchmark_architecture.py --iterations 5000 --output /tmp/motion-benchmark.json
```

测试默认不调用真实Qwen、外部MCP或摄像头；包括本地WebRTC回环。真实服务使用现有 `scripts/smoke_qwen_realtime.py` 并先查看 `--help`，显式配置测试密钥后再运行。本轮没有执行真实服务smoke，不把环境变量本身当作所有脚本都已遵循的安全门。

## 5. 运行库与存储检查

```bash
uv run python -c "import sys, sqlite3; print(sys.version); print(sqlite3.sqlite_version); c=sqlite3.connect(':memory:'); print(c.execute('select sqlite_source_id()').fetchone()[0])"
```

CI测得SQLite3.45.1。生产使用WAL前核实 [SQLite官方WAL-reset修复说明](https://www.sqlite.org/wal.html#walreset) 和发行商补丁；3.51.3及后续含上游修复，官方亦列特定回移版本。版本号落在受影响范围不等于已经发生损坏，也不能排除发行商回移补丁。本轮未改变Python链接的SQLite引擎。

停止应用并完成writer关闭后进行备份，或使用SQLite备份API；不要在应用运行时只复制主DB并丢弃WAL文件。队列完整性失败的session不能当作完整训练用于比较或总结。

## 6. 初次手工验收

先仅使用深蹲模式验证本地计数。其他UI动作尚没有对应权威FSM，不能用来证明通用运动识别。验证暂停时持续来帧也不计数；换流后不拼接动作；用户开始说话时旧提示被撤销；网络不可用时本地暂停仍生效。

需要另测实际摄像头、麦克风、扬声器、AEC、冷启动、音频播放ACK、真实网络和磁盘负载。自动测试通过不能替代这些体验与安全验收。

## 7. Rollback Plan

尚未合并时，停止测试实例，切回原main即可；不要reset远端分支或force push。为了独立比较，可以创建只读baseline工作区：

```bash
git worktree add --detach ../motion-relay-main-baseline f2f104d3c280972bc9e3aecb75fdafe8c76a4f85
```

若后续维护者已合并，则使用独立rollback分支按逻辑commit执行revert并重跑测试。没有schema迁移，不需要降级数据库表；已有新增状态记录应保留为审计历史，不能为了让旧代码显示“成功”而删除。

回滚会重新引入本轮修复的暂停、跨流和完整性问题，应明确告知使用者。不要将回滚理解为安全问题自动消失。

## 8. Known Limitations

原生Qwen对话仍旁路仲裁；迟到response.created关联尚不完整；无可靠played_at；operation池只限制单owner，不能强杀线程；真正端到端latency与用户效果未测；exercise/provider插件、全链路tracing和生产存储加固未完成。
