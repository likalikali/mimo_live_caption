# MiMo Live Caption 1.0.22

Windows 桌面实时字幕工具。可同时采集麦克风与电脑声音，显示可选择、可复制的悬浮字幕，并按需调用云端 ASR、翻译、自动导出 TXT/SRT。

## 1.0.22 新增：Qwen-Audio 3.0 多语种低延迟流式 ASR

本版新增独立识别平台：

```text
Qwen-Audio 3.0 ASR Flash Streaming（多语种按量付费）
```

使用的模型固定为：

```text
qwen-audio-3.0-asr-flash-streaming
```

它与旧的 `qwen3-asr-flash-realtime` 是两套不同 WebSocket 协议，不能只在原配置里替换模型名称。本版会尝试迁移旧 Qwen provider、Workspace、地域、语言和常见 Windows 凭据项；升级后仍建议打开设置确认一次。本版已实现新的 DashScope 双向流协议：

```text
建立 WebSocket
→ run-task
→ 收到 task-started
→ 每约 100 ms 发送 16 kHz / 16-bit / mono PCM
→ result-generated(sentence_end=false) 原位更新临时字幕
→ result-generated(sentence_end=true) 定稿
→ finish-task
→ task-finished 后复用连接
```

服务端实际返回粒度由模型决定，通常会按字、词或短语连续更新；程序收到每一条中间结果后立即刷新，不会等待整句。

## Qwen 配置

打开：

```text
设置 → 转录与音频 → 识别服务
```

选择：

```text
API 平台：Qwen-Audio 3.0 ASR Flash Streaming（多语种按量付费）
```

填写：

- **百炼 API Key**：阿里云百炼对应地域的 API Key。也可以设置环境变量 `DASHSCOPE_API_KEY`。
- **地域**：华北 2（北京）或新加坡。API Key、Workspace 和地域必须一致。
- **Workspace ID**：业务空间 ID，例如 `llm-xxxxxxxxxxxxxxxx`。
- **识别语言**：
  - `自动检测`：不发送 `language_hints`；
  - `zh,en,ja`：限定中文、英文、日语；
  - 也可手动输入最多四个语言代码，例如 `zh,en,ja,ko`。
- **服务端判停**：推荐 600–800 ms；数值越小定稿越快，也更容易切碎。
- **即时热词**：每行或逗号分隔，例如 `Moonshot AI、Kimi K3、清结算`。

北京地域连接地址由程序自动生成：

```text
wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference
```

新加坡地域连接地址：

```text
wss://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/inference
```

API Key 会优先保存到 Windows 凭据管理器，不明文写入普通配置 JSON。

## 多语种

Qwen-Audio 3.0 Streaming 可自动识别或使用语言提示，适合中文、英语、日语、韩语及更多语种。单一视频建议明确指定语种；中英日混合或频繁切换时可选择自动检测或 `zh,en,ja`。

自动检测并不保证每个短音频片段都能准确判断语种。短句、背景音乐或口音较重时，明确指定语言通常更稳定。

## 与现有引擎的关系

- **Qwen-Audio 3.0 Streaming**：多语种低延迟中间结果，适合作为统一中英日主引擎。
- **豆包流式 ASR 2.0**：中文/英文低延迟双向流和服务端二遍定稿。
- **Google Speech-to-Text V2**：日语 Chirp 3 增量流式备用。
- **MiMo / SiliconFlow**：文件式或批量定稿识别。
- **sherpa-onnx**：本地逐词预览备用。

选择 Qwen-Audio 3.0 后，程序不会再同时启动本地 sherpa 或 MiMo/SiliconFlow 二次校正，避免两套识别结果相互覆盖和重复计费。

## 音频与字幕行为

- 麦克风、电脑声音可单独或同时采集；两路同时开启会分别产生识别用量。
- 默认自动匹配 Windows 麦克风和 WASAPI Loopback，也可在下拉框手动固定设备。
- 设备下拉框忽略鼠标滚轮，避免滚动设置页时误切换。
- 未定稿字幕会在同一行原位更新；定稿后可按完整句子换行。
- 框选字幕时暂停界面文档改写，ASR 仍在后台运行，因此 partial 也能稳定选择并右键复制。
- 向上查看历史时暂停自动跟随；拖回底部或点击“回到最新”后恢复跟随。

## 翻译

支持：

- Google Cloud Translation Basic v2；
- OpenAI 兼容接口（LM Studio、GLM 等）。

推荐使用“稳定整句”或“仅翻译定稿”，避免持续 partial 重复消耗字符额度。显示模式位于：

```text
设置 → 字幕翻译 → 原文与译文显示
```

可选“原文 + 译文 / 仅原文 / 仅译文”。

## 自动导出

在：

```text
设置 → 导出与记录
```

可开启停止转录时自动导出：

- 原稿 TXT；
- 带时间戳 SRT；
- 可选在 SRT 中附加译文。

默认保存到程序目录下的 `exports` 子文件夹。

## 安装与运行

需要 Windows 10/11 和 Python 3.10+。

```bat
run.bat
```

脚本会创建 `.venv` 并安装 `requirements.txt`。

手动安装：

```bat
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python app.py
```

## 环境变量

可复制 `.env.example` 中的名称到系统环境变量：

```text
DASHSCOPE_API_KEY=
DOUBAO_ASR_API_KEY=
MIMO_API_KEY=
SILICONFLOW_API_KEY=
GOOGLE_TRANSLATION_API_KEY=
OPENAI_TRANSLATION_API_KEY=
GOOGLE_CLOUD_PROJECT=
GOOGLE_APPLICATION_CREDENTIALS=
```

程序不会自动读取 `.env` 文件；这些名称用于说明可设置的系统环境变量。

## 网络代理

在“设置 → 网络与代理”中填写 HTTP、SOCKS5 或 mixed-port，并选择应用到语音识别 API。Qwen 使用 WebSocket，可通过 `websocket-client` 的 HTTP/SOCKS 代理连接。

修改代理后建议停止识别并重新开始，使新的 WebSocket 使用最新路由。

## 常见问题

### 仍显示 `qwen3-asr-flash-realtime`

说明仍在运行旧版程序或旧配置界面。1.0.22 的 Qwen 平台模型框应固定显示：

```text
qwen-audio-3.0-asr-flash-streaming
```

请完全退出程序，解压完整新版，并确认 `qwen_streaming.py` 与 `app.py` 位于同一目录。

### 缺少 Workspace ID

进入阿里云百炼业务空间，复制当前空间的 Workspace ID。它不是项目名称，也不是 API Key。

### 401 / 403

通常是 API Key 地域与 Workspace 地域不一致、Key 无权访问当前业务空间、账户未开通服务或余额/结算异常。

### 连接失败

检查代理应用范围、防火墙以及以下域名：

```text
*.cn-beijing.maas.aliyuncs.com
*.ap-southeast-1.maas.aliyuncs.com
```

### 不是严格“一字一帧”

公开接口返回连续的中间识别结果，但不会承诺每个汉字、假名或英文单词都单独形成一次网络事件。程序不会人为等待；收到服务端更新就立即显示。

## 隐私与费用

启用云端识别后，所选音源的语音会发送到相应云服务商。请在录制会议、通话或他人声音前确认授权与当地法律要求。

Qwen-Audio 3.0 Streaming 按处理音频时长计费。麦克风和电脑声音同时开启时，两路分别计费。价格与优惠可能变化，请以百炼控制台为准。

## 本版文件

```text
app.py
qwen_streaming.py
doubao_streaming.py
google_speech_streaming.py
requirements.txt
run.bat
build_exe.bat
```

## 验证范围

发布包已进行 Python 语法编译、Qwen 请求载荷、服务端事件映射、语言提示、即时热词和 ZIP 完整性检查。当前构建环境没有 Windows WASAPI、用户百炼凭据和真实付费接口，因此首次真实连接仍需在你的 Windows 电脑上验证。
