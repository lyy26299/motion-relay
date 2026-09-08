# 全双工浏览器 AEC：实现与验收

更新：2026-09-07。适用于本项目 `agent_local.py` 桌面入口，基于回退后的无回声处理版本实现。

## 选择结论

本项目采用浏览器 WebRTC 内置 AEC，优先打开 Chrome。麦克风与摄像头由浏览器采集，教练语音通过同一个 RTCPeerConnection 返回浏览器扬声器播放。麦克风在教练说话期间持续工作，支持双讲和用户插话。

这是针对当前项目的工程选择，不是对所有 AEC 算法做效果排名。原来的 LocalEdge 使用独立本地录音/播放流，没有 AEC；自行接入原生 WebRTC AEC 还需要维护播放参考、采样时钟、设备延迟与原生依赖。浏览器可直接管理 WebRTC 的采集和渲染路径。

`echoCancellation: true` 是请求浏览器启用回声消除，不等于强制某个操作系统 AEC，也不保证浏览器固定使用某个 AEC3 版本。实际底层实现由浏览器和设备决定。代码检查 `audioTrack.getSettings().echoCancellation === true`；没有确认启用时停止连接并提示处理，不静默降级到无 AEC。

## 媒体链路

```text
麦克风 → 浏览器 AEC / 降噪 / 自动增益 → WebRTC 音频上行
                                               ↓
                           Python BrowserEdge → Agent → Qwen
                                               ↑          ↓
摄像头 → 浏览器 WebRTC 视频上行 → 视频分发 / YOLO     PCM 24 kHz
                                    ↓                     ↓
                               桌面姿态画面      重采样 / WebRTC 音频下行
                                                          ↓
                                               同一浏览器的扬声器播放
```

音频约束放在 `getUserMedia` 的 `audio` 字段中：

```javascript
audio: {
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
  channelCount: 1
}
```

本地预览只包含视频轨道，且保持 muted，避免把自己的麦克风再次播放。Python 不打开 sounddevice 或本地摄像头，桌面设备枚举已移除。原模型、语音和 VAD 阈值配置继续使用原值；AEC 不通过调高 VAD 或播放时关闭麦克风实现。

## 文件与职责

| 文件 | 职责 |
| --- | --- |
| `coach/browser_edge.py` | 实现 EdgeTransport、本机 HTTP 信令、WebRTC 音视频事件、处理后画面转交桌面、会话清理 |
| `coach/browser_media.html` | 请求设备权限与 AEC、检查实际设置、协商连接、播放教练语音、释放设备 |
| `coach/qwen_duplex.py` | 对固定版本 SDK 补齐打断和输出完成事件，过滤取消响应的音频，修复读任务取消后连接未清理的问题 |
| `agent_local_agent.py` | 创建浏览器 Edge，等待媒体连接后进入训练，处理用户停止、浏览器断开与授权超时 |
| `agent_local.py` | 保留训练控制和姿态画面，将设备入口改为浏览器授权说明 |
| `tests/test_browser_edge.py` | 合成音频、双向 WebRTC、鉴权和 Qwen 生命周期回归测试 |
| `scripts/smoke_browser_aec.py` | 使用真实 Chrome、虚拟设备和离线 Qwen 替身验证完整 Agent 媒体链路 |

直接依赖固定为 `aiohttp==3.14.3`、`aiortc==1.14.0`、`av==16.1.0`，已同步 `uv.lock`。HTML 已加入 Python package data。SDK 继续固定 `vision-agents==0.6.9`；升级 SDK 时需要复查 `DuplexQwenRealtime` 的事件桥接。

## 全双工与打断

音频输出使用 48 kHz 单声道、20 ms 常规帧，空闲发送静音并保持 RTP 时间戳递增。重采样器在正常响应结束时排出尾部样本，避免末尾音节被截断。

播放队列最多容纳 25 帧，满时向上游施加背压。打断会清空 Agent 的输出缓冲，再清空 WebRTC 输出队列和重采样器；代次检查防止被背压挂起的旧写入恢复后重新放入音频。已经发往浏览器的 RTP/设备缓冲无法由 Python 撤回，插话后仍可能有短暂尾音，其长度取决于浏览器抖动缓冲和设备延迟。

原 Qwen SDK 只在服务端仍生成时取消响应。新适配器在 speech_started 时总是发出 interrupted 输出事件，因此服务端生成结束、浏览器尚在播放时也可清理本地积压。取消后丢弃旧 response_id 的音频，正常 response.done 则发送结束标记。

## 启动与使用

在项目目录运行：

```bash
uv sync --locked --extra dev
.venv/bin/python agent_local.py
```

1. 点击桌面的“开始训练”，等待模型加载。
2. 浏览器自动打开；点击“连接麦克风与摄像头”并授权。
3. 页面应显示“已连接 · 回声消除已启用 · 支持同时说话”，桌面进入训练状态。
4. 教练声音在浏览器页面播放。若自动播放被阻止，点击页面的音频播放控件。
5. 点击桌面停止或浏览器结束媒体连接，均可结束会话。再次训练请从桌面重新开始，使用新的会话链接。

默认尝试 Chrome，失败时尝试系统默认浏览器；可通过 `COACH_BROWSER` 指定 Python webbrowser 支持的浏览器名称。若未自动打开，可复制桌面运行记录里的完整链接。媒体设备在浏览器的站点权限中选择，声音输出默认跟随浏览器/系统设置。

信令服务仅监听 `127.0.0.1` 的随机端口，每次会话使用随机令牌，检查同源请求，不设置公网 STUN/TURN。服务只接受一个浏览器连接。Qwen API key 留在 Python 进程中。页面用于同一台电脑，不支持手机或远程浏览器直接访问。

## 已完成的自动验证

运行命令：

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/smoke_browser_aec.py
```

2026-09-07 的结果：14 项离线回归测试全部通过。覆盖重采样/尾帧、单调时间戳、静音帧、清空重采样残留、背压中打断和停止、无效令牌/来源/AEC 状态、真实本机 WebRTC 双向音频与视频、重复连接拒绝、取消后的音频过滤、正常结束标记、Qwen 读任务和连接关闭，以及既有 Qwen 合约。

真实 Chrome 冒烟使用独立临时配置与虚拟麦克风/摄像头，不采集真实设备，不调用云端 Qwen、不加载 YOLO。通过真实 Agent 的分发流程和 Qwen 的音视频输入方法，把云端网络客户端替换为离线计数器；下行经过真实 Agent 的语音输出流程。

一次实际结果：

```json
{
  "aec": true,
  "connected": "connected",
  "speakerPlaying": true,
  "previewAudioTracks": 0,
  "uplink_pcm_frames": 96,
  "agent_audio_frames": 95,
  "agent_video_frames": 1,
  "stop_cleanup": true
}
```

同时确认浏览器 audio/video 上行 bytesSent 与 audio 下行 bytesReceived 均大于零。帧数会随调度变化，测试判断链路和行为，不要求每次完全相同。

## 真实设备验收

自动测试证明代码链路和浏览器设置有效，不能证明真实房间的回声抑制量；尚未测量 ERLE、双讲失真，也未在本轮调用云端进行真人通话。

用你日常的麦克风与扬声器完成以下检查：

| 场景 | 期望 |
| --- | --- |
| 教练连续说话，用户保持安静 | 不将教练声音反复识别为用户话语，不产生自问自答 |
| 教练说话时，用户正常音量插话 | 用户语音仍被识别，积压的教练语音停止，随后回应用户 |
| 连续对话数分钟 | 播放延迟不持续增长，没有反复触发回声循环 |
| 关闭页面、停止训练、拔掉设备 | 会话结束，摄像头/麦克风占用释放；新会话可重新连接 |
| 浏览器拒绝权限或未确认 AEC | 明确提示，结束此次连接，可从桌面重新开始 |

若真实双讲效果仍不理想，先记录浏览器版本、音频设备、扬声器音量和识别结果，并在 Chrome 的 `chrome://webrtc-internals` 观察相应连接。不要把“getSettings 返回 true”当作声学效果已经验收。

## 参考文档

- [W3C Media Capture and Streams](https://www.w3.org/TR/mediacapture-streams/)：echoCancellation 约束与浏览器对其含义的处理。
- [aiortc API Reference](https://aiortc.readthedocs.io/en/latest/api.html)：RTCPeerConnection、轨道与 SDP 协商接口。
- [aiortc 官方 server 示例](https://github.com/aiortc/aiortc/blob/main/examples/server/server.py)：HTTP offer/answer 与媒体轨道的实现参考。
