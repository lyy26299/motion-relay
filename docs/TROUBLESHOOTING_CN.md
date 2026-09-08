# 故障排除指南

## 当前运行方式

当前桌面入口是 `agent_local.py`，不是旧版 `vision_agent_demo.py`。

macOS/Linux：

```bash
./run.sh
```

Windows：

```bat
scripts\\run.bat
```

启动前可运行配置检查；它不会打印 API key：

```bash
.venv/bin/python scripts/check_local_setup.py
```

项目没有 Docker、独立前端或常驻 Web 服务。`./run.sh` 启动的是 Tk 桌面控制器，
浏览器页面由运行时的 BrowserEdge 临时提供；MCP server 是另一个独立的 stdio 进程。

离线检查（不调用 Qwen 云端）：

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/smoke_pose.py --device cpu
.venv/bin/python scripts/smoke_browser_aec.py --pose --pose-device cpu
```

## 启动前检查

1. 使用 Python 3.13，项目要求 `>=3.13,<3.14`。
2. 在仓库根目录执行 `uv sync --locked`。
3. 复制 `.env.example` 为 `.env`，填写 `DASHSCOPE_API_KEY`。
4. 确认 `yolo11n-pose.pt` 存在；不存在时首次启动会自动下载。
5. 使用 Chromium，并允许本机 BrowserEdge 页面访问摄像头和麦克风。
6. 如需持久化多位训练者，设置 `COACH_USER_ID`；如需更换账本位置，设置
   `COACH_MEMORY_DB`。默认分别为 `local-user` 和 `coach_memory.sqlite3`。

## 常见问题

### `DASHSCOPE_API_KEY` 未配置

检查 `.env` 是否存在，以及变量不是占位值。不要在日志、截图或提交中暴露 key。

```bash
grep '^DASHSCOPE_API_KEY=' .env
```

### 浏览器没有自动打开

查看终端中的本机 URL，通常形如 `http://127.0.0.1:端口/?token=...`，然后手动复制到 Chromium。该 URL 是一次性会话地址，不要分享给其他人。

### 摄像头或麦克风无法访问

- 在浏览器地址栏允许摄像头和麦克风权限。
- 关闭 Zoom、Teams 等占用设备的程序。
- 刷新本机 BrowserEdge 页面并重新授权。
- 页面必须确认 AEC 已启用，否则服务端会拒绝 SDP offer。

### YOLO 模型下载失败

先单独运行：

```bash
.venv/bin/python scripts/smoke_pose.py --device cpu
```

如果网络下载失败，可手动获取 `yolo11n-pose.pt` 后放在仓库根目录，再重试。也可以在 `.env` 设置 `YOLO_DEVICE=cpu` 降低设备兼容性问题。

### 没有语音反馈或 Qwen 返回 401/403/429

确认 DashScope 账户已开通 Qwen Realtime，检查 `DASHSCOPE_BASE_URL` 和 `QWEN_REALTIME_MODEL`。浏览器音量、播放权限和麦克风权限也必须正常。先用离线 smoke 测试确认本地媒体链路，再排查云端权限。

### AI 看不到动作或角度显示为 `--`

- 保持单人、全身在画面内，并改善光线。
- 侧面/斜侧机位更适合深蹲膝角观测。
- 第二人进入画面、关键点置信度不足或姿态过期时，系统会暂停角度和计数，这是保护行为。
- 当前动作事实以本地 YOLO + 深蹲 FSM 为准，不能用 Qwen 的自然语言计数替代。

### 进程卡住或关闭不干净

先按桌面窗口的停止按钮；必要时在终端按 `Ctrl+C`，等待清理后重新运行 `./run.sh`。不要同时启动多个桌面实例占用摄像头。

### CPU 占用过高

在 `.env` 中设置：

```dotenv
YOLO_DEVICE=cpu
```

保持 nano 模型和默认有界队列；不要通过提高 Qwen 帧率来修复动作计数，计数由本地 FSM 负责。

## 调试日志

```bash
LOG_LEVEL=DEBUG .venv/bin/python agent_local.py
```

仅记录会话状态和错误，不要把 `.env` 内容重定向到日志。

## 项目当前边界

- 已实现并测试：YOLO 结构化姿态快照、单人深蹲 FSM、短期工作记忆、SQLite WAL
  事实账本、后台写入队列、停止时 drain、浏览器 AEC loopback。
- `coach.mcp_server`、检索服务和有限步 `coach.agent_loop` 已有离线契约测试，
  但尚未由桌面会话自动拉起；Qwen 工具调用、跨会话问答和完整 RAG 仍按 roadmap 接入。
- 因此 `./run.sh` 当前保证的是“实时姿态 + 本地权威计数 + 事实落盘”，不能把独立
  MCP server 或设计文档误认为完整 Agent Loop 已上线。
