# MiMo Live Caption 1.0.21
## 1.0.21：未定稿字幕可稳定复制与设置归位

### 未定稿字幕复制

实时 partial 仍会持续进入内存中的字幕数据模型，但用户按下左键开始框选后，界面文档暂时停止改写。选区因此不会被下一次 partial 替换或打断。

- 左键开始框选时立即暂停界面刷新；
- 选区保持高亮期间继续暂停；
- 右键直接复制，或按 `Ctrl+C`，复制后解除暂停；
- 暂停期间收到的新 partial 只保留最新状态，不逐帧补刷；
- 解除暂停后一次性显示最新字幕；
- 开始选择前若处于自动跟随状态，复制完成后恢复自动跟随；若原本在阅读历史，则保持原位置。

这只冻结字幕的显示文档，不暂停录音、ASR、翻译或导出时间记录。

### 设置界面

“原文 + 译文 / 仅原文 / 仅译文”已从“转录与音频 → 字幕外观”移动到：

```text
设置 → 字幕翻译 → 原文与译文显示
```

显示时间戳、音源标签、定稿换行、颜色和透明度仍留在“转录与音频 → 字幕外观”。

### 日语增量识别的准确表述

- 豆包公开的优化双向流 `bigmodel_async` 目前仍只支持中英文；日语走 `bigmodel_nostream`，属于流式上传、句级返回，不是逐字双向流。
- Google Cloud Speech-to-Text V2 的 `StreamingRecognize` 支持 `ja-JP`，并能返回可能变化的 interim 结果；但 API 不保证每个词、每个假名都单独返回一次，实际通常按字词或短词组更新。
- Gboard 等输入法能呈现更接近逐字的体验，通常还结合设备端模型、输入法上下文、个性化与界面层的稳定前缀处理；这不等同于公开云 API 对每个字的返回粒度作出保证。

因此设置中的 Google 日语引擎改名为“低延迟增量，不保证逐字”，避免把 interim results 误写成严格逐字协议。

## 1.0.20：Google Speech gRPC 代理修复

本版修复 Google 日语实时识别在需要本地代理的网络中仍然直连 `us-speech.googleapis.com:443`，并出现 `503 ... tcp handshaker shutdown` 的问题。

设置方法：

1. 打开“设置 → 网络与代理”。
2. 开启“启用自定义本地代理”。
3. 地址填写 HTTP 或混合端口，例如 `http://127.0.0.1:20800`。
4. 勾选“语音识别 API（豆包 / Google Speech gRPC / MiMo / SiliconFlow）”。
5. 保存后停止并重新开始识别。

Google Speech 使用 gRPC 的 HTTP CONNECT 隧道，不能直接使用 `socks5://` 或 `https://` 形式的代理地址。Clash/Mihomo 等客户端应填写其 HTTP 或 mixed-port。

日志中应出现：

```text
Google Japanese streaming enabled ... proxy=http://127.0.0.1:20800
Google Speech gRPC client endpoint=us-speech.googleapis.com:443 proxy=http://127.0.0.1:20800
```

若仍显示 `proxy=direct`，说明“语音识别 API”代理范围没有勾选。


本版增加 Google Cloud Speech-to-Text V2 的日语低延迟流式识别，并修复向上阅读后无法重新恢复自动跟随的问题。

## 日语低延迟流式识别

明确选择“日语”时，可以选择：

- **Google Cloud Speech-to-Text V2（Chirp 3，低延迟增量，不保证逐字）**：使用 `StreamingRecognize` 和 `interim_results`，字幕会在讲话过程中原位增长；
- **豆包日语分块**：保留为备用，不需要 Google Speech 凭据，但不是逐字词双向流。

中文和英语仍使用豆包 `bigmodel_async`，不会因为加入 Google 日语而改变原有低延迟链路。自动语种检测仍使用豆包多语种分块模式。

Google 的 interim 结果通常按字词或短词组增量更新，但服务端不保证每个单独假名都会独立返回一次。

## Google Cloud 配置

1. 在 Google Cloud 项目中启用 **Cloud Speech-to-Text API** 并关联结算账号。
2. 推荐给专用服务账号授予 **Cloud Speech Client** 角色。
3. 在软件中打开：

   `设置 → 转录与音频 → 豆包流式 ASR 2.0`

4. 设置：

   - 识别语言：`日语`
   - 日语实时引擎：`Google Cloud Speech-to-Text V2（Chirp 3）`
   - Google Project ID：填写项目 ID；服务账号 JSON 中已有时可留空
   - Google 凭据：选择服务账号 JSON；留空则使用 Application Default Credentials
   - Google 区域：`us` 或 `eu`

Google 翻译 API Key 不能用于 Speech-to-Text。两项服务的 API、权限和费用相互独立。

使用 ADC 时，可先在命令提示符运行：

```bat
gcloud auth application-default login
```

然后在设置中填写项目 ID，凭据路径留空。

如果启动时缺少 `google-cloud-speech`、找不到凭据或项目 ID，且已经配置豆包凭据，程序会明确提示并自动退回豆包日语分块。本地配置正确但云端返回权限、结算或网络错误时会显示真实错误，不会静默伪装成成功。

## 自动跟随修复

- 向上滚动查看历史时，自动跟随暂停；
- 将滚动条拖回底部，或用滚轮滚到最底部后，自动跟随立即恢复；
- 新 partial 使文档高度继续增长时，滚动条会保持贴住最新内容；
- “回到最新”按钮仍可一键恢复；
- 框选文字不会被强制取消。

## 依赖与升级

新增依赖：

```text
google-cloud-speech>=2.40,<3
```

从 1.0.18 升级时，建议直接使用完整压缩包。手动覆盖至少需要：

- `app.py`
- `google_speech_streaming.py`
- `requirements.txt`
- `build_exe.bat`（需要打包 EXE 时）

运行 `run.bat` 会根据 `requirements.txt` 安装新增依赖。

## 注意

Google StreamingRecognize 单条流最长约 5 分钟。本版在连续无停顿音频接近限制前自动轮换会话；正常句间停顿仍会及时定稿。Google 与豆包均按各自规则计费，麦克风和电脑声音同时开启时是两路识别。