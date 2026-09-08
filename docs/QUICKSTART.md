# 快速开始指南

## 5 分钟快速部署

### 第一步：准备环境

- Python 3.13（`pyproject.toml` 要求 `>=3.13,<3.14`）
- macOS/Linux：安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Chromium 浏览器，并允许摄像头和麦克风
- 一个启用了 Qwen Realtime 的 DashScope API key（[阿里云百炼](https://bailian.console.aliyun.com/)）

### 第二步：安装和配置

```bash
cd vision-agent-real-demo
uv sync --locked
cp .env.example .env
```

编辑 `.env`，只需填写以下必需项（不要把真实密钥提交到 Git）：

```dotenv
DASHSCOPE_API_KEY=your_key_here
```

可选项包括 `DASHSCOPE_BASE_URL`、`QWEN_REALTIME_MODEL`、`QWEN_VOICE`、
`YOLO_DEVICE`、`COACH_USER_ID` 和 `COACH_MEMORY_DB`。其中 `COACH_USER_ID`
用于隔离训练者的长期记录，`COACH_MEMORY_DB` 是 SQLite 账本路径（默认
`coach_memory.sqlite3`）。

### 第三步：启动

```bash
# macOS/Linux（根目录或 scripts/ 下的脚本均可）
./run.sh
# 或
./scripts/run.sh

# Windows PowerShell/CMD
scripts\run.bat
```

脚本会先运行本地配置检查（不会打印密钥），然后执行 `.venv/bin/python agent_local.py`
（Windows 使用项目虚拟环境中的 `.venv\\Scripts\\python.exe`）。桌面窗口启动后，在浏览器页面授权摄像头和麦克风，
再点击“开始训练”。首次运行若缺少 `yolo11n-pose.pt`，Ultralytics 会自动下载约 6 MB 权重。

直接启动（跳过脚本检查）可用：

```bash
.venv/bin/python agent_local.py
```

## 记忆账本与 MCP 诊断

每次训练会创建独立 `session_id`。YOLO 姿态经过本地深蹲 FSM 后，权威动作事件、
完成次数和证据窗口由后台 `LedgerWriter` 写入 `COACH_MEMORY_DB`；停止或窗口关闭时
会先排空队列，再将 session 标记为 `completed` 或 `interrupted`。默认不保存原始音频和视频。

MCP server 是独立的 stdio 诊断入口，不会由桌面程序自动启动，也不要把它当作前端服务：

```bash
.venv/bin/python -m coach.mcp_server \
  --db coach_memory.sqlite3 \
  --user-id local-user
```

该进程会等待 MCP client 输入；需要检索历史时应由受控 client 连接。当前桌面主链路已
接入 YOLO/FSM/短期记忆/SQLite 账本，但 `coach.agent_loop`、MCP client、RAG 问答和
Qwen 工具调用仍按 roadmap 分阶段接入，不能把独立 server 启动误认为 Agent Loop 已上线。

## 验证命令

不需要摄像头或云端 key 的回归：

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/smoke_pose.py --device cpu
.venv/bin/python scripts/smoke_browser_aec.py --pose --pose-device cpu
```

可选的真实 Qwen 协议 smoke 会产生云端调用费用，仅在确认 key 和权限后执行：

```bash
RUN_QWEN_LIVE_TEST=1 .venv/bin/python scripts/smoke_qwen_realtime.py
```

## 常见问题（1 分钟解决）

### Q: 提示 "DASHSCOPE_API_KEY 未配置"
**A**: 检查 `.env` 文件是否存在，且 `DASHSCOPE_API_KEY` 不是占位值（不要带引号或多余空格）。

### Q: 浏览器没有自动打开
**A**: 在桌面窗口中点击“开始训练”后，程序会打开 BrowserEdge 页面；若未自动打开，查看终端日志中的本地 URL。

### Q: 摄像头无法访问
**A**:
- 确保浏览器有摄像头权限
- 关闭其他占用摄像头的程序
- 刷新浏览器页面重新授权

### Q: YOLO 模型下载慢
**A**: 首次运行会自动下载约 6MB 模型，稍等片刻。如果网络慢可以手动下载：
```bash
wget https://github.com/ultralytics/assets/releases/download/v0.0.0/yolo11n-pose.pt
```

### Q: Python 版本不对
**A**: 当前锁定依赖要求 Python 3.13（不支持 3.14+）。
```bash
# 检查版本
python3 --version

# macOS 升级
brew install python@3.13

# Ubuntu/Debian
sudo apt install python3.13
```

## 测试清单

启动成功的标志：
- [ ] 终端显示 "Agent 已加入通话"
- [ ] 浏览器打开视频界面
- [ ] 可以看到自己的摄像头画面
- [ ] AI 发出语音问候
- [ ] 做动作后 AI 有反馈

## 下一步

✅ **自定义 AI 行为**: 编辑 `docs/COACHING_INSTRUCTIONS.md`
✅ **调整性能**: 在 `.env` 设置 `YOLO_DEVICE`、`QWEN_REALTIME_MODEL` 或 `QWEN_VOICE`
✅ **添加功能**: 查看 `docs/COACH_AGENT_MEMORY_MCP_RAG_DESIGN.md` 与 roadmap

---

**遇到问题？**
- 查看完整 README.md
- GitHub Issues: https://github.com/GetStream/Vision-Agents/issues
- DashScope 文档: https://help.aliyun.com/zh/model-studio/

**享受你的 AI 视觉交互体验！** 🎉
