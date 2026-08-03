# 1.0.21

- 用户开始框选字幕时暂停 QTextDocument 改写，解决未定稿 partial 不断替换导致难以选中和右键复制的问题。
- 选区期间 ASR 与翻译继续运行，只缓存每条字幕的最新显示状态；复制或取消选择后一次性刷新。
- 右键快速复制和 `Ctrl+C` 都会完成复制、解除选择并恢复刷新。
- 选择前处于自动跟随时，复制后恢复贴底；阅读历史时保持原滚动位置。
- 将“原文 + 译文 / 仅原文 / 仅译文”移动到“字幕翻译”标签页。
- Google 日语引擎文案改为“低延迟增量，不保证逐字”，避免把 interim results 误解为逐假名保证。

# 1.0.20

- Google Speech-to-Text V2 改为显式创建带 `grpc.http_proxy` 的授权 gRPC channel，避免客户端延迟建连后绕过代理。
- OAuth 凭据刷新使用同一个显式 HTTP 代理会话。
- 网络设置中的语音识别代理范围明确包含 Google Speech gRPC 和豆包。
- 对 `failed to connect to all addresses`、`tcp handshaker shutdown` 提供可操作的中文提示。
- 日志新增 Google Speech endpoint 与实际代理路由。

# 1.0.19

- 新增 Google Cloud Speech-to-Text V2 `chirp_3` 日语实时流式引擎。
- 使用 `StreamingRecognize`、`ja-JP`、LINEAR16 16 kHz 和 `interim_results=True`。
- Google interim 与 final 复用现有字幕槽位、定稿拆句、翻译及 SRT 时间链路。
- 中文和英语继续使用豆包 `bigmodel_async`，不改变原有逐字词体验。
- Google 本地依赖或凭据不可用时，自动退回豆包日语分块。
- 新增 Google Project ID、凭据 JSON、区域和日语引擎设置。
- 新增 `google-cloud-speech` 依赖及 PyInstaller 收集配置。
- Google 流式消息使用约 200 ms / 6400 字节音频包；连续会话在 5 分钟限制前轮换。
- 修复滚动条拖回底部后仍不恢复自动跟随的问题。
- 文档高度变化时进行延迟二次贴底，避免新 partial 把阅读位置留在旧的最大值。
