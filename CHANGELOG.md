# 1.0.22

- 新增独立识别平台 **Qwen-Audio 3.0 ASR Flash Streaming（多语种按量付费）**。
- 模型固定为 `qwen-audio-3.0-asr-flash-streaming`，不再把旧的 `qwen3-asr-flash-realtime` 模型名直接套用到不同协议。
- 新增 DashScope WebSocket 双向流实现：`run-task → task-started → binary PCM → result-generated → finish-task → task-finished`。
- 每约 100 ms 发送 16 kHz、16-bit、单声道 PCM；每个 `sentence_end=false` 中间结果立即原位更新字幕。
- `sentence_end=true` 进入现有定稿、按句换行、翻译、时间戳和 TXT/SRT 导出链路。
- 支持北京和新加坡业务空间，自动根据地域与 Workspace ID 生成 WebSocket 地址。
- 支持自动语种检测及最多四个 `language_hints`，预设中文、英语、日语、韩语组合。
- 支持 Qwen-Audio 3.0 即时热词，默认使用保守权重 5。
- 支持 WebSocket HTTP/SOCKS 代理、连接复用、任务级错误提示和计费时长日志。
- Qwen API Key 保存到独立 Windows 凭据项，也支持环境变量 `DASHSCOPE_API_KEY` / `QWEN_ASR_API_KEY`。
- 增加旧 `qwen3-asr-flash-realtime` provider、配置字段和常见 Keyring 账户的升级迁移。
- Qwen 模式禁用本地 sherpa 与批量云端二次校正，避免双模型输出冲突和重复调用。
- 保留 1.0.21 的未定稿字幕稳定选择复制、阅读锁定与自动跟随恢复等功能。

# 1.0.21

- 用户开始框选字幕时暂停 QTextDocument 改写，解决未定稿 partial 不断替换导致难以选中和右键复制的问题。
- 选区期间 ASR 与翻译继续运行，只缓存每条字幕的最新显示状态；复制或取消选择后一次性刷新。
- 右键快速复制和 `Ctrl+C` 都会完成复制、解除选择并恢复刷新。
- 选择前处于自动跟随时，复制后恢复贴底；阅读历史时保持原滚动位置。
- 将“原文 + 译文 / 仅原文 / 仅译文”移动到“字幕翻译”标签页。
- Google 日语引擎文案改为“低延迟增量，不保证逐字”，避免把 interim results 误解为逐假名保证。
