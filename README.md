# 多模型实时字幕 0.6.0

Windows 实时字幕悬浮窗，可同时捕获并转写：

- 麦克风声音，显示为 `[我]`
- 电脑播放声音，显示为 `[会议]`

字幕可以鼠标框选并按 `Ctrl+C` 复制。电脑声音继续使用只输入的 WASAPI Loopback，不会接管耳机或扬声器播放。

## 0.6.0：智能语音分片

默认不再每隔固定秒数机械上传。程序使用 WebRTC VAD 和自适应背景噪声阈值处理两个音源：

1. 平时仅保留约 240 毫秒的预缓存。
2. 检测到连续讲话后进入“正在收音”。
3. 连续静音达到“结束停顿”时，立即结束并上传这一段语音。
4. 结尾只保留少量静音，减少上传数据和模型等待。
5. 一直不停顿时，达到“最长语音段”后自动切分，并保留少量重叠，避免丢字。

默认参数：

- VAD 灵敏度：`2 - 平衡`
- 结束停顿：`600 毫秒`
- 最长语音段：`10 秒`
- 开头预缓存：`240 毫秒`

在口语对话中，一句话结束约 0.6 秒后就会提交，不必等待原来的 4 秒固定边界。

## 流式文字返回

设置中的“模型支持时使用流式文字返回”默认开启。

### MiMo

`mimo-v2.5-asr` 支持流式响应。程序在语音段上传完成后读取 SSE 增量文字，并在同一条字幕中不断更新；最终响应完成后去掉末尾的省略号。

注意：这是“上传完整语音段后，服务端流式返回文字”，不是持续向服务器推送麦克风音频。因此，本地 VAD 和较短的最长语音段仍然决定首次请求何时开始。

### SiliconFlow

SiliconFlow 当前公开的 `/v1/audio/transcriptions` 接口返回一次性 JSON：

```json
{"text": "..."}
```

官方文档没有提供该转写接口的流式参数，所以以下模型仍在整段识别完成后一次显示：

- `FunAudioLLM/SenseVoiceSmall`
- `TeleAI/TeleSpeechASR`

不过智能 VAD 会在停顿后立刻上传，仍比固定分片更快。

## 分片设置建议

### 普通会议和对话

- 分片方式：智能语音分片
- VAD 灵敏度：2
- 结束停顿：600 毫秒
- 最长语音段：10 秒

### 说话较慢、句中停顿较多

把结束停顿调到 `900` 或 `1200` 毫秒，避免一句话被切成多段。

### 环境噪声大或视频含大量音乐

把 VAD 灵敏度调到 `3`。如果吞掉轻声内容，再退回 `2` 或 `1`。

### 追求更低延迟

可把结束停顿调到 `350` 或 `450` 毫秒，并把最长语音段调到 `6` 或 `8` 秒。代价是请求次数增加，句子更容易被拆开。

### VAD 与特殊音频不兼容

“分片方式”可以临时切换为“固定时长（兼容模式）”。固定模式保留旧版行为。

## 支持的 API 平台

### MiMo

- 模型：`mimo-v2.5-asr`
- 环境变量：`MIMO_API_KEY`
- 支持流式文字响应

### SiliconFlow

- `FunAudioLLM/SenseVoiceSmall`
- `TeleAI/TeleSpeechASR`
- 环境变量：`SILICONFLOW_API_KEY`
- 默认 API 地址：`https://api.siliconflow.cn/v1`

API Key 分别保存到 Windows 凭据管理器，互不覆盖。


## 音频设置

建议保持：

- 转录麦克风（我）
- 转录电脑声音（会议/视频）
- 麦克风兼容模式
- 防止 Windows 降低或静音其他应用声音

电脑声音请选择当前耳机或扬声器对应的 `[Loopback]` 项。

## 状态提示

窗口标题旁会显示：

- `等待说话`
- `正在收音：麦克风`
- `正在收音：电脑`
- `正在收音：麦克风 + 电脑`

API 状态可能显示：

- `MiMo: 正在识别麦克风`
- `MiMo: 流式返回中`
- `MiMo: 正常`
- `SiliconFlow: 正在识别电脑声音`

日志中会记录每段的实际时长和触发原因：

```text
Adaptive segment source=mic seconds=2.34 rms=... reason=silence
Adaptive segment source=system seconds=10.00 rms=... reason=max_duration
```

## 快捷键

- `Ctrl+C`：复制框选文字
- `Ctrl+Shift+C`：复制全部
- `Ctrl+L`：清空
- `Ctrl+Space`：开始或停止

## 日志

```text
%APPDATA%\MiMoLiveCaption\app.log
```



## 接口文档

- MiMo ASR：https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/audio/Speech-Recognition
- SiliconFlow ASR：https://docs.siliconflow.cn/cn/api-reference/audio/create-audio-transcriptions
- WebRTC VAD wheels：https://pypi.org/project/webrtcvad-wheels/
