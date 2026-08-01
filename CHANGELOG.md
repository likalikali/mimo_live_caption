# Changelog

## 0.6.0

- Replaced fixed-duration segmentation with adaptive voice activity segmentation by default.
- Added WebRTC VAD with adaptive background-energy gating.
- Added configurable VAD aggressiveness, end-of-speech pause, and maximum utterance duration.
- Added pre-roll, post-roll, minimum-speech filtering, and max-duration overlap.
- Added VAD state feedback: idle, speaking, and segment submitted.
- Added MiMo SSE streaming response parsing and live in-place subtitle updates.
- Kept SiliconFlow transcription non-streaming because its public transcription endpoint currently documents only a final `text` response.
- Retained fixed-duration segmentation as a compatibility fallback.
- Added `webrtcvad-wheels` dependency and PyInstaller collection rule.

## 0.5.0

- Added selectable ASR providers: MiMo and SiliconFlow.
- Added SiliconFlow multipart audio transcription support.
- Added `FunAudioLLM/SenseVoiceSmall` and `TeleAI/TeleSpeechASR` presets.
- Added editable SiliconFlow model ID and API base URL.
- Added separate Windows Credential Manager entries for MiMo and SiliconFlow keys.
- Added provider/model information to logs and status labels.
- Preserved all 0.4.0 WASAPI loopback and Windows ducking fixes.

## 0.4.0

- Fixed possible loss of playback audio while microphone and loopback capture are active.
- Enforced input-only streams (`output=False`) for microphone and WASAPI loopback.
- Added MME/DirectSound microphone compatibility remapping.
- Added one-time Windows communication ducking opt-out prompt.
- Replaced blocking reads with PortAudio callbacks.
- Improved deterministic stream shutdown and logging.
