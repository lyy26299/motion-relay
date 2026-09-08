# Vision Agent Real Demo 🤖💪

> **专业 AI 健身教练** - 实时视觉 AI、姿态检测和语音反馈驱动

[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![Vision-Agents](https://img.shields.io/badge/vision--agents-0.2.10-green.svg)](https://github.com/GetStream/Vision-Agents)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![YOLO](https://img.shields.io/badge/YOLO-v11-red.svg)](https://github.com/ultralytics/ultralytics)

**[English](../README.md)** | **[快速开始](./QUICKSTART.md)** | **[故障排除](./TROUBLESHOOTING_CN.md)**

---

## 🎯 这是什么？

一个使用计算机视觉和语音 AI 指导你健身的**实时 AI 教练**。基于官方 [GetStream/Vision-Agents](https://github.com/GetStream/Vision-Agents) 框架构建，结合了：

- 🎥 **实时视频分析** - 本机 BrowserEdge WebRTC（浏览器负责摄像头、麦克风和 AEC）
- 🦴 **姿态检测** - YOLO 11 追踪 17 个身体关键点
- 🧠 **AI 教练** - Qwen Realtime（DashScope）
- 🗣️ **语音反馈** - 即时纠正动作和鼓励

### 实际演示

<p align="center">
  <img src="./images/demo-preview.png" alt="演示预览" width="800"/>
</p>

> *AI 教练实时分析深蹲，检测膝盖对齐并提供即时反馈*

---

## ✨ 功能特性

### 🏋️ 专业健身指导

- **5 大核心动作** 详细动作分析
  - 深蹲 - 下肢力量之王
  - 俯卧撑 - 上肢综合力量
  - 平板支撑 - 核心稳定之基
  - 弓步蹲 - 单腿力量与平衡
  - 卷腹 - 腹部核心训练

- **实时动作纠正**
  - 检测 17 个身体关键点
  - 计算关节角度
  - 识别常见错误（膝盖内扣、弓背等）
  - 即时语音反馈

- **渐进式训练**
  - 难度等级：初级 → 中级 → 高级
  - 动作质量计数
  - 基于表现的个性化建议

### 🔬 技术优势

- **低延迟本地链路** - BrowserEdge WebRTC + 有界 YOLO 队列
- **精确姿态检测** - YOLO 11 Pose（17 个关键点）
- **多模态 AI** - Qwen Realtime（视觉 + 音频）
- **可审计动作事实** - 本地深蹲 FSM、短期工作记忆和 SQLite 长期账本正在逐步接入

---

## 🚀 快速开始

### 前置要求

- Python 3.13（要求 `>=3.13,<3.14`）
- Chromium 浏览器，并允许摄像头和麦克风
- DashScope Qwen Realtime API 密钥（[阿里云百炼](https://bailian.console.aliyun.com/)）

### 安装

```bash
# 1. 克隆仓库
git clone https://github.com/MindDock/vision-agent-real-demo.git
cd vision-agent-real-demo

# 2. 安装 uv（快速包管理器）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 3. 安装锁定依赖
uv sync --locked

# 4. 配置 Qwen Realtime API 密钥和本地账本（可选）
cp .env.example .env
nano .env  # 至少填写 DASHSCOPE_API_KEY；COACH_USER_ID/COACH_MEMORY_DB 可按需调整

# 5. 启动本地训练台
./run.sh
```

脚本会先运行配置检查，然后启动 `agent_local.py`。桌面窗口打开后，点击“开始训练”，
再在随后打开的 Chromium 页面中授权摄像头和麦克风。默认会将 YOLO/FSM 动作事实写入
`coach_memory.sqlite3`；可用 `COACH_USER_ID` 和 `COACH_MEMORY_DB` 配置用户隔离与账本路径。

离线验证（不调用云端）：

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/smoke_pose.py --device cpu
.venv/bin/python scripts/smoke_browser_aec.py --pose --pose-device cpu
```

📖 **详细指南**: 查看 [QUICKSTART.md](./QUICKSTART.md)

当前桌面主链路已接入短期工作记忆、后台 SQLite 事实账本和停止时排空；MCP/RAG 与
有限步 Agent Loop 已有离线契约实现，但尚未由桌面会话自动拉起。详见
[实施路线](./COACH_IMPLEMENTATION_ROADMAP.md)。

---

## 🎬 工作原理

```
┌─────────────┐         ┌──────────────┐         ┌─────────────┐
│  浏览器     │  WebRTC │ BrowserEdge  │ 处理    │ Python      │
│  (摄像头)   │────────▶│  网络        │────────▶│  Agent      │
│             │         │ (< 30ms)     │         │             │
└─────────────┘         └──────────────┘         └──────┬──────┘
                                                        │
                                                        ▼
                                          ┌─────────────────────┐
                                          │ YOLO 姿态检测       │
                                          │ (17 个关键点)       │
                                          └──────────┬──────────┘
                                                     │
                                                     ▼
                                          ┌─────────────────────┐
                                          │ Qwen Realtime       │
                                          │ (视觉 + 语音)       │
                                          └──────────┬──────────┘
                                                     │
                                                     ▼
                                          ┌─────────────────────┐
                                          │ 语音反馈            │
                                          │ "膝盖向外推！"      │
                                          └─────────────────────┘
```

### 架构说明

1. **视频捕获** - 你的摄像头通过 WebRTC 传输视频
2. **本地传输** - BrowserEdge 在本机提供 WebRTC 和回声消除
3. **姿态检测** - YOLO 每帧提取 17 个身体关键点
4. **AI 分析** - Qwen Realtime 处理视频和音频；动作计数以本地 FSM 为准
5. **语音指导** - 实时音频反馈引导你的动作

---

## 🏃 使用示例

### 开始训练

```
你："我想做深蹲"
AI："很好！双脚与肩同宽，脚尖微微外展。
     我会看着你的动作。准备好了吗？开始！"

[你做深蹲]
AI："深度不错！但注意膝盖 - 有点内扣。
     把膝盖向外推。我们再来一个！"

[你调整后再做]
AI："完美！这是第 1 个。膝盖对齐，深度到位。继续！"
```

### 实时纠正

```
[做俯卧撑时]
AI："等等 - 你的臀部下沉了。收紧核心！
     想象你的身体是一块木板，从头到脚一条直线。"

[你纠正姿势]
AI："好多了！就是这样。保持呼吸 - 下降时吸气，
     推起时呼气。"
```

### 训练计划建议

```
你："今天应该练什么？"
AI："根据上次训练，我们重点练下肢：
     • 深蹲：3 组 × 12 个
     • 弓步蹲：3 组 × 10 个（每腿）
     • 平板支撑：3 组 × 45 秒

     准备好开始了吗？"
```

---

## 📊 AI 能看到什么

### YOLO 关键点（检测 17 个）

```
1. 鼻子
2-3. 左眼、右眼
4-5. 左耳、右耳
6-7. 左肩、右肩
8-9. 左肘、右肘
10-11. 左腕、右腕
12-13. 左髋、右髋
14-15. 左膝、右膝
16-17. 左踝、右踝
```

### AI 分析指标

- ✅ **关节角度** - 膝盖、肘部、髋部屈曲
- ✅ **身体对齐** - 肩-髋-膝-踝直线
- ✅ **动作深度** - 深蹲深度、俯卧撑幅度
- ✅ **错误检测** - 膝盖内扣、弓背等
- ✅ **质量评分** - 每个动作 A/B/C/D 评分

---

## 🛠️ 配置

### 调整性能

**调整性能**（在 `.env` 中设置）:
```python
YOLO_DEVICE=mps  # 没有 MPS 时使用 cpu
QWEN_REALTIME_MODEL=qwen3.5-omni-plus-realtime
QWEN_VOICE=Ethan
```

### 自定义 AI 行为

编辑 `docs/COACHING_INSTRUCTIONS.md` 更改：
- 教练风格（严格/鼓励/技术型）
- 运动重点（力量/有氧/灵活性）
- 反馈详细程度（简洁/详细）

旧版 Stream/Gemini 示例仅作历史参考，不是当前 `agent_local.py` 的运行依赖。

---

## 📁 项目结构

```
vision-agent-real-demo/
├── agent_local.py                 # Tk 桌面主入口
├── agent_local_agent.py           # 会话生命周期和 Qwen/YOLO 接线
├── pyproject.toml                 # 依赖配置
├── .env.example                   # API 密钥模板
│
├── docs/
│   ├── README_CN.md               # 中文文档
│   ├── QUICKSTART.md              # 快速开始
│   ├── TROUBLESHOOTING_CN.md      # 故障排除
│   ├── COACHING_INSTRUCTIONS.md   # AI 教练指令
│   ├── COACH_AGENT_MEMORY_MCP_RAG_DESIGN.md # 记忆/MCP/RAG 设计
│   └── images/                    # 截图和演示
│
├── scripts/
│   ├── run.sh                     # 启动脚本（macOS/Linux）
│   ├── run.bat                    # 启动脚本（Windows）
│   └── setup.sh                   # 设置脚本
│
└── tests/                         # 离线、姿态、动作和账本测试
```

---

## 🤝 贡献

欢迎贡献！以下是你可以帮助的方式：

### 改进方向

- 🎨 **添加 AR 可视化** - 在视频上绘制骨骼叠加层
- 🏃 **更多运动** - 添加波比跳、引体向上、瑜伽姿势
- 🌍 **国际化** - 支持更多语言
- 📱 **移动应用** - iOS/Android 原生应用
- 🎯 **自定义训练计划** - 周计划、目标设定
- 📊 **进度追踪** - 保存历史记录、显示改进

### 如何贡献

1. Fork 仓库
2. 创建功能分支（`git checkout -b feature/amazing-feature`）
3. 提交更改（`git commit -m '添加惊人功能'`）
4. 推送到分支（`git push origin feature/amazing-feature`）
5. 开启 Pull Request

详细指南见 [CONTRIBUTING.md](../CONTRIBUTING.md)。

---

## 📝 许可证

本项目采用 MIT 许可证 - 详见 [LICENSE](../LICENSE) 文件。

---

## 🙏 致谢

- **[GetStream/Vision-Agents](https://github.com/GetStream/Vision-Agents)** - 官方框架
- **[Ultralytics YOLO](https://github.com/ultralytics/ultralytics)** - 姿态检测模型
- **[Qwen Realtime](https://help.aliyun.com/zh/model-studio/realtime)** - 多模态 AI
- **BrowserEdge/WebRTC** - 本地浏览器媒体传输

---

## 📞 支持

- 📖 **文档**: [docs/](.)
- 🐛 **Bug 报告**: [GitHub Issues](https://github.com/MindDock/vision-agent-real-demo/issues)
- 💬 **讨论**: [GitHub Discussions](https://github.com/MindDock/vision-agent-real-demo/discussions)

---

## 📈 路线图

- [x] 核心健身指导（5 个动作）
- [x] 实时姿态检测
- [x] 语音反馈系统
- [ ] AR 骨骼可视化
- [ ] 进度追踪面板
- [ ] 自定义训练计划
- [ ] 移动应用（iOS/Android）
- [ ] 多语言支持
- [ ] 社交功能（分享训练）
- [ ] 高级分析

---

<p align="center">
  <strong>由 <a href="https://github.com/MindDock">MindDock</a> 用 ❤️ 构建</strong>
</p>

<p align="center">
  <sub>基于 Vision-Agents • YOLO • Qwen Realtime • BrowserEdge</sub>
</p>
