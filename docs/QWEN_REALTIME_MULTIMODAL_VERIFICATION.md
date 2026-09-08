# Qwen Realtime 多模态输入能力验证

验证日期：2026-09-07  
验证对象：`qwen3.5-omni-plus-realtime`  
验证接口：`wss://dashscope.aliyuncs.com/api-ws/v1/realtime`  
验证协议：WebSocket

## 结论

实测确认 `qwen3.5-omni-plus-realtime` 可以在同一会话、同一轮响应上下文中联合处理：

- 音频输入；
- 图片输入（即视频抽帧）；
- 普通文本 `input_text`。

原生 PDF/DOCX 文件输入未得到支持。服务端可以接受包含 `input_file` 的消息外壳，但模型并未获得可读取的 PDF 内容。因此，“请求被协议接受”不能视为“模型支持文档理解”。

当前项目安装的 Vision-Agents 0.6.9 Qwen 适配器会主动拒绝普通文本输入。这是适配器限制，不是本次实测模型后端的限制。

## 官方文档依据

阿里云 Qwen-Omni-Realtime 文档说明，该模型能够同时理解流式音频与图像输入；图像可以来自视频流抽帧：

- <https://help.aliyun.com/zh/model-studio/realtime>

同一页面还说明可以通过 `conversation.item.create` 发送 `input_text`。但客户端事件参考页面写明 `conversation.item.create` 当前仅支持 `function_call_output`：

- <https://help.aliyun.com/zh/model-studio/client-events>

两份官方说明存在不一致，因此本项目对实际端点进行了最小调用验证。

## 验证环境

| 项目 | 值 |
|---|---|
| 模型 | `qwen3.5-omni-plus-realtime` |
| 接口 | `wss://dashscope.aliyuncs.com/api-ws/v1/realtime` |
| Vision-Agents | `0.6.9` |
| websockets | `15.0.1` |
| 会话输出 | `text` |
| Turn Detection | `null`，手动提交 |
| 测试音频 | 本地合成的 16 kHz、单声道、PCM16 语音 |
| 测试图片 | 本地生成的 JPEG，包含文字 `AV TEST` |
| 测试文档 | 本地生成的 PDF，包含标记 `PDF_7291` |

测试不输出 API Key，也没有修改远端数据。使用手动提交模式是为了精确控制一轮中包含的输入，不代表项目最终必须关闭服务端 VAD。

## 测试矩阵与结果

| 测试 | 服务端是否接受 | 模型是否正确理解 | 结果 |
|---|---:|---:|---|
| 音频 + 图片 | 是 | 是 | 完整返回 `response.done`，正确识别图片内容 |
| 普通 `input_text` | 是 | 是 | 按要求返回 `TEXT_OK` |
| 音频 + 图片 + 文本 | 是 | 是 | 返回 `AUDIO=4815; IMAGE=AV TEST` |
| 原生 PDF `input_file` | 消息外壳被接受 | 否 | 模型表示没有收到可读取的 PDF |

## 关键实测记录

### 1. 音频和图片共同提交

发送顺序：

1. `input_audio_buffer.append`
2. `input_image_buffer.append`
3. `input_audio_buffer.commit`
4. `response.create`

服务端依次产生了以下关键事件：

```text
input_audio_buffer.committed
conversation.item.created
conversation.item.input_audio_transcription.completed
response.created
response.text.delta
response.text.done
response.done
```

结果：音频与图片缓冲区被共同提交，模型成功生成响应。

### 2. 普通文本输入

发送的核心事件：

```json
{
  "type": "conversation.item.create",
  "item": {
    "type": "message",
    "role": "user",
    "content": [
      {
        "type": "input_text",
        "text": "Reply only TEXT_OK"
      }
    ]
  }
}
```

服务端返回 `conversation.item.created`，调用 `response.create` 后模型正确返回：

```text
TEXT_OK
```

这说明针对本次验证的模型和端点，普通 `message + input_text` 实际可用。

### 3. 音频、图片和文本联合理解

同一轮中发送：

- 音频语义：数字 `4815`；
- 图片内容：`AV TEST`；
- 文本任务：要求分别报告音频数字与图片文字。

模型实际返回：

```text
AUDIO=4815; IMAGE=AV TEST
```

这不仅证明三种输入被服务端接收，也证明模型在同一响应中使用了音频语义、图像语义和文本指令。

### 4. PDF 文件输入

测试通过 `conversation.item.create` 发送：

```json
{
  "type": "message",
  "role": "user",
  "content": [
    {
      "type": "input_file",
      "filename": "probe.pdf",
      "file_data": "<base64>"
    },
    {
      "type": "input_text",
      "text": "Read the attached PDF and reply only with its secret code."
    }
  ]
}
```

服务端产生了 `conversation.item.created`，但模型回复：

```text
No PDF was attached to your message. Please upload the file so I can attempt to read it.
```

PDF 内的标记 `PDF_7291` 未被读取。因此，当前端点不应被设计为支持原生文件输入。

## 对当前项目的影响

### 当前已经具备的能力

`agent_local_agent.py` 中已经启用：

```python
qwen.Realtime(
    fps=1,
    include_video=True,
    ...
)
```

当前 Qwen 客户端通过以下事件把媒体发往模型：

- 音频：`input_audio_buffer.append`
- 图片：`input_image_buffer.append`

所以当前项目具备音频与视频抽帧联合输入的基础。

### 当前缺失的能力

Vision-Agents 0.6.9 的 Qwen 适配器在 `simple_response()` 中直接提示不支持文本输入并返回空结果。对应位置：

```text
.venv/lib/python3.13/site-packages/vision_agents/plugins/qwen/qwen_realtime.py
```

因此当前状态是：

```text
Qwen 模型/API：实测支持 input_text
Vision-Agents 适配器：没有暴露该能力
应用代码：暂时无法通过 simple_response(text=...) 使用该能力
```

不要直接修改 `.venv`。应在项目内添加适配层，或向 Vision-Agents 上游提交修复。

## 推荐实现

在项目自己的 Qwen 适配器中增加文本事件发送能力：

```python
async def send_text_event(client, text: str) -> None:
    await client.send_event(
        {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": text}
                ],
            },
        }
    )
```

教练系统可以据此输入结构化事件：

```text
YOLO / Exercise FSM
        ↓
结构化 CoachEvent
        ↓ input_text
Qwen Realtime ← 音频 + 视频抽帧
        ↓
文本 / 音频反馈
```

实现时还需要：

1. 串行化用户语音轮次与 `CoachEvent`，避免同时触发两个响应。
2. 配合 `response.cancel` 和音频缓冲清理实现真正的用户打断。
3. 给事件增加过期时间，避免补播旧的动作纠正。
4. 保留 FSM 产生的权威计数；不能让 LLM 修改真实 `rep_count`。
5. 安全事件优先使用固定模板，不依赖开放式生成成功。

## 文档输入策略

PDF、DOCX 等文档不要直接使用 `input_file` 发送给 Realtime API。推荐：

- 简短、固定规则：解析成文本后放入 `session.instructions`。
- 实时动作阈值和安全规则：写入确定性代码或 YAML 配置。
- 长篇训练知识：在外部做解析、分块和检索，将相关片段作为 `input_text` 发送。
- 强依赖页面布局的内容：只转换必要页面为图片输入，不发送整份文档。

## 验证边界

本结论严格限定于上述日期、模型和端点：

- 测试使用 WebSocket，而不是直接调用阿里云 WebRTC。
- 联合输入测试使用手动提交模式；项目现有 `server_vad` 下仍需做轮次调度集成测试。
- 测试证明普通文本事件在当前端点可用，但阿里云后续可能调整未完全公开的事件契约。
- PDF 测试证明本次端点未向模型提供文件内容；不能据此推断未来所有模型版本都不支持。
- 视频能力指连续图像帧理解，不等于逐帧、全帧率的视频推理。

## 最终决策

1. 将 Qwen Realtime 作为音频、视频抽帧和文本事件的统一实时模型使用。
2. 在项目内补齐 `input_text` 适配，不修改虚拟环境文件。
3. 文档先解析或检索，再转换为文本/图片；不使用原生 `input_file`。
4. 计数、安全和动作状态继续由确定性 FSM 负责，LLM 只消费结构化事实并生成反馈。
