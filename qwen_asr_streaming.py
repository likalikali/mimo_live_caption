from __future__ import annotations

import base64
from collections import deque
import json
import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import websocket
from PySide6.QtCore import QObject, Signal


SUPPORTED_QWEN_ASR_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("自动检测", "auto"),
    ("中文（普通话及部分方言）", "zh"),
    ("粤语", "yue"),
    ("英语", "en"),
    ("日语", "ja"),
    ("德语", "de"),
    ("韩语", "ko"),
    ("俄语", "ru"),
    ("法语", "fr"),
    ("葡萄牙语", "pt"),
    ("阿拉伯语", "ar"),
    ("意大利语", "it"),
    ("西班牙语", "es"),
    ("印地语", "hi"),
    ("印尼语", "id"),
    ("泰语", "th"),
    ("土耳其语", "tr"),
    ("乌克兰语", "uk"),
    ("越南语", "vi"),
)

LANGUAGE_NAMES = {code: label for label, code in SUPPORTED_QWEN_ASR_LANGUAGES}


@dataclass(frozen=True)
class QwenAsrStreamingConfig:
    api_key: str
    region: str = "beijing"
    workspace_id: str = ""
    endpoint: str = ""
    model: str = "qwen3-asr-flash-realtime"
    language: str = "auto"
    sample_rate: int = 16_000
    silence_duration_ms: int = 400
    vad_threshold: float = 0.0
    packet_ms: int = 100
    proxy_url: str = ""
    connect_timeout: float = 10.0
    final_timeout: float = 10.0
    idle_close_seconds: float = 20.0


class QwenAsrStreamingSignals(QObject):
    partial = Signal(str, float, str)
    final = Signal(str, float, str)
    segment_timing = Signal(str, float, float)
    status = Signal(str, str)
    failed = Signal(str, str)
    detected_language = Signal(str, str)


def normalize_qwen_region(region: str) -> str:
    value = str(region or "beijing").strip().lower()
    aliases = {
        "cn": "beijing",
        "china": "beijing",
        "cn-beijing": "beijing",
        "beijing": "beijing",
        "sg": "singapore",
        "intl": "singapore",
        "international": "singapore",
        "ap-southeast-1": "singapore",
        "singapore": "singapore",
    }
    if value not in aliases:
        raise ValueError("Qwen3-ASR 地域必须是北京或新加坡。")
    return aliases[value]


def build_qwen_realtime_endpoint(config: QwenAsrStreamingConfig) -> str:
    custom = str(config.endpoint or "").strip()
    if custom:
        if not custom.startswith("wss://"):
            raise ValueError("Qwen3-ASR WebSocket 地址必须以 wss:// 开头。")
        base = custom
    else:
        region = normalize_qwen_region(config.region)
        workspace = str(config.workspace_id or "").strip()
        if workspace:
            if not all(ch.isalnum() or ch == "-" for ch in workspace):
                raise ValueError("Qwen3-ASR Workspace ID 含有无效字符。")
            suffix = (
                "cn-beijing.maas.aliyuncs.com"
                if region == "beijing"
                else "ap-southeast-1.maas.aliyuncs.com"
            )
            base = f"wss://{workspace}.{suffix}/api-ws/v1/realtime"
        else:
            # Legacy regional domains remain usable. Workspace-specific domains
            # are preferred by Alibaba Cloud for improved stability.
            base = (
                "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
                if region == "beijing"
                else "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime"
            )

    parsed = urlparse(base)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["model"] = str(config.model or "qwen3-asr-flash-realtime")
    return urlunparse(parsed._replace(query=urlencode(query)))


def _proxy_kwargs(proxy_url: str, endpoint_host: str) -> dict[str, Any]:
    value = str(proxy_url or "").strip()
    if not value:
        return {"http_no_proxy": [endpoint_host]}
    if "://" not in value:
        value = "http://" + value
    parsed = urlparse(value)
    if not parsed.hostname or not parsed.port:
        raise ValueError("代理地址缺少主机或端口。")
    scheme = parsed.scheme.lower()
    if scheme in {"http", "https"}:
        proxy_type = "http"
    elif scheme in {"socks5", "socks5h"}:
        proxy_type = scheme
    else:
        raise ValueError("WebSocket 代理仅支持 HTTP、SOCKS5 或 SOCKS5H。")
    result: dict[str, Any] = {
        "http_proxy_host": parsed.hostname,
        "http_proxy_port": parsed.port,
        "proxy_type": proxy_type,
    }
    if parsed.username:
        result["http_proxy_auth"] = (parsed.username, parsed.password or "")
    return result


def combine_qwen_preview(stable_text: str, stash_text: str) -> str:
    """Return the provider's complete current hypothesis.

    The API contract explicitly defines ``text`` as the confirmed prefix and
    ``stash`` as the revisable suffix. They are adjacent pieces of one sentence and
    must be concatenated without adding an artificial separator.
    """
    return f"{stable_text or ''}{stash_text or ''}".strip()


class QwenAsrStreamingWorker:
    """Persistent Qwen3-ASR Realtime WebSocket worker for one audio source."""

    def __init__(
        self,
        source: str,
        config: QwenAsrStreamingConfig,
        signals: QwenAsrStreamingSignals,
    ) -> None:
        self.source = source
        self.config = config
        self.signals = signals
        self.jobs: queue.Queue[tuple[str, bytes, float]] = queue.Queue(maxsize=4096)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"QwenAsrStreaming-{source}",
            daemon=True,
        )
        self._ws: Optional[websocket.WebSocket] = None
        self._receiver: Optional[threading.Thread] = None
        self._receiver_stop = threading.Event()
        self._session_ready = threading.Event()
        self._session_finished = threading.Event()
        self._send_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._send_buffer = bytearray()
        self._session_wall_start = 0.0
        self._session_audio_seconds = 0.0
        self._audio_spans: deque[tuple[float, float, float]] = deque(maxlen=8192)
        self._segment_byte_offsets: dict[float, int] = {}
        self._item_starts: dict[str, float] = {}
        self._item_ends: dict[str, float] = {}
        self._item_last_preview: dict[str, str] = {}
        self._fallback_item_start = 0.0
        self._latest_local_end = 0.0
        self._last_language = ""
        self._idle_deadline: Optional[float] = None
        self._fatal_error = ""

    def start(self) -> None:
        self._thread.start()

    def feed(self, pcm: bytes, timestamp: float) -> None:
        if self._stop.is_set() or not pcm:
            return
        try:
            self.jobs.put_nowait(("audio", bytes(pcm), float(timestamp)))
        except queue.Full:
            self.signals.failed.emit(
                self.source, "Qwen3-ASR 实时音频队列已满；当前连接将重置。"
            )
            self._reset_connection()

    def end(self, timestamp: float) -> None:
        if self._stop.is_set():
            return
        try:
            self.jobs.put_nowait(("end", b"", float(timestamp)))
        except queue.Full:
            self._reset_connection()

    def stop(self) -> None:
        if self._stop.is_set():
            return
        try:
            self.jobs.put(("stop", b"", time.time()), timeout=0.5)
        except queue.Full:
            self._stop.set()
        # Allow session.finish to deliver the final completed event before the
        # socket is force-closed. This is especially important when the user stops
        # during the last unfinished utterance.
        join_timeout = min(12.0, max(4.0, float(self.config.final_timeout) + 1.0))
        self._thread.join(timeout=join_timeout)
        if self._thread.is_alive():
            self._stop.set()
            self._reset_connection()

    def _record_audio_span(self, pcm: bytes, segment_timestamp: float) -> None:
        duration = len(pcm) / 2.0 / float(self.config.sample_rate)
        if duration <= 0:
            return
        segment_key = round(float(segment_timestamp), 6)
        segment_offset_bytes = self._segment_byte_offsets.get(segment_key, 0)
        wall_start = float(segment_timestamp) + (
            segment_offset_bytes / 2.0 / float(self.config.sample_rate)
        )
        audio_start = self._session_audio_seconds
        audio_end = audio_start + duration
        self._audio_spans.append((audio_start, audio_end, wall_start))
        self._session_audio_seconds = audio_end
        self._segment_byte_offsets[segment_key] = segment_offset_bytes + len(pcm)
        self._fallback_item_start = min(
            self._fallback_item_start or wall_start, wall_start
        )

    def _wall_time_for_audio_offset(self, seconds: float) -> float:
        offset = max(0.0, float(seconds))
        with self._state_lock:
            spans = list(self._audio_spans)
            session_start = self._session_wall_start
        for start, end, wall_start in reversed(spans):
            if start <= offset <= end + 0.001:
                return wall_start + max(0.0, offset - start)
        if spans:
            start, end, wall_start = spans[-1]
            if offset > end:
                return wall_start + (end - start)
        return session_start or time.time()

    def _send_json(self, payload: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise RuntimeError("Qwen3-ASR WebSocket 尚未连接。")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._send_lock:
            ws.send(encoded)

    def _session_update_event(self) -> dict[str, Any]:
        transcription: dict[str, Any] = {}
        language = str(self.config.language or "auto").strip().lower()
        if language and language != "auto":
            transcription["language"] = language
        session: dict[str, Any] = {
            "modalities": ["text"],
            "input_audio_format": "pcm",
            "sample_rate": int(self.config.sample_rate),
            "turn_detection": {
                "type": "server_vad",
                "threshold": float(self.config.vad_threshold),
                "silence_duration_ms": max(
                    200, min(6000, int(self.config.silence_duration_ms))
                ),
            },
        }
        if transcription:
            session["input_audio_transcription"] = transcription
        return {
            "event_id": f"event_{uuid.uuid4().hex}",
            "type": "session.update",
            "session": session,
        }

    def _open_connection(self, timestamp: float) -> None:
        self._reset_connection()
        api_key = str(self.config.api_key or "").strip()
        if not api_key:
            raise RuntimeError("缺少阿里云百炼 API Key。")
        endpoint = build_qwen_realtime_endpoint(self.config)
        host = urlparse(endpoint).hostname or "dashscope.aliyuncs.com"
        kwargs = _proxy_kwargs(self.config.proxy_url, host)
        headers = [
            f"Authorization: Bearer {api_key}",
            "OpenAI-Beta: realtime=v1",
        ]
        self.signals.status.emit(self.source, "正在连接 Qwen3-ASR Realtime")
        ws = websocket.create_connection(
            endpoint,
            header=headers,
            timeout=float(self.config.connect_timeout),
            enable_multithread=True,
            **kwargs,
        )
        ws.settimeout(1.0)
        self._ws = ws
        self._receiver_stop.clear()
        self._session_ready.clear()
        self._session_finished.clear()
        self._fatal_error = ""
        self._session_wall_start = float(timestamp or time.time())
        self._session_audio_seconds = 0.0
        self._audio_spans.clear()
        self._segment_byte_offsets.clear()
        self._item_starts.clear()
        self._item_ends.clear()
        self._item_last_preview.clear()
        self._fallback_item_start = float(timestamp or time.time())
        self._latest_local_end = 0.0
        self._last_language = ""
        self._idle_deadline = None
        self._receiver = threading.Thread(
            target=self._receive_loop,
            name=f"QwenAsrReceive-{self.source}",
            daemon=True,
        )
        self._receiver.start()
        self._send_json(self._session_update_event())
        if not self._session_ready.wait(timeout=4.0):
            if self._fatal_error:
                raise RuntimeError(self._fatal_error)
            raise RuntimeError("Qwen3-ASR 会话配置超时。")
        logging.info(
            "Qwen3-ASR connected source=%s endpoint=%s model=%s language=%s proxy=%s",
            self.source,
            endpoint,
            self.config.model,
            self.config.language,
            self.config.proxy_url or "direct",
        )
        self.signals.status.emit(self.source, "已连接，正在多语种低延迟增量识别")

    def _flush_audio(self, *, force: bool = False) -> None:
        packet_bytes = max(
            640,
            int(self.config.sample_rate * 2 * max(20, int(self.config.packet_ms)) / 1000),
        )
        while len(self._send_buffer) >= packet_bytes or (force and self._send_buffer):
            take = packet_bytes if len(self._send_buffer) >= packet_bytes else len(self._send_buffer)
            chunk = bytes(self._send_buffer[:take])
            del self._send_buffer[:take]
            self._send_json(
                {
                    "event_id": f"event_{uuid.uuid4().hex}",
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode("ascii"),
                }
            )

    def _friendly_error(self, exc: Exception) -> str:
        text = str(exc).strip() or exc.__class__.__name__
        lowered = text.lower()
        if "401" in lowered or "unauthorized" in lowered or "invalid api" in lowered:
            return "Qwen3-ASR 鉴权失败，请确认 API Key 与所选地域一致。原始错误：" + text
        if "403" in lowered or "permission" in lowered:
            return "Qwen3-ASR 无权限，请确认百炼业务空间已开通模型并允许该 API Key 调用。原始错误：" + text
        if "proxy" in lowered or "handshake" in lowered or "timed out" in lowered:
            return "Qwen3-ASR WebSocket 网络连接失败，请检查代理、节点和地域地址。原始错误：" + text
        return text

    def _handle_message(self, data: dict[str, Any]) -> None:
        event_type = str(data.get("type") or "")
        if event_type in {"session.created", "session.updated"}:
            if event_type == "session.updated":
                self._session_ready.set()
            return
        if event_type == "session.finished":
            self._session_finished.set()
            return
        if event_type == "error":
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            code = str(error.get("code") or error.get("type") or "error")
            message = str(error.get("message") or data)
            self._fatal_error = f"Qwen3-ASR 错误 {code}: {message}"
            self._session_ready.set()
            self.signals.failed.emit(self.source, self._fatal_error)
            return
        if event_type == "conversation.item.input_audio_transcription.failed":
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            code = str(error.get("code") or "transcription_failed")
            message = str(error.get("message") or "语音识别失败")
            self.signals.failed.emit(
                self.source, f"Qwen3-ASR 错误 {code}: {message}"
            )
            return
        if event_type == "input_audio_buffer.speech_started":
            item_id = str(data.get("item_id") or "")
            if item_id:
                start_ms = float(data.get("audio_start_ms") or 0.0)
                self._item_starts[item_id] = self._wall_time_for_audio_offset(
                    start_ms / 1000.0
                )
            return
        if event_type == "input_audio_buffer.speech_stopped":
            item_id = str(data.get("item_id") or "")
            if item_id:
                end_ms = float(data.get("audio_end_ms") or 0.0)
                self._item_ends[item_id] = self._wall_time_for_audio_offset(
                    end_ms / 1000.0
                )
            return
        if event_type not in {
            "conversation.item.input_audio_transcription.text",
            "conversation.item.input_audio_transcription.completed",
        }:
            return

        item_id = str(data.get("item_id") or "anonymous")
        language = str(data.get("language") or "").strip()
        if language and language != self._last_language:
            self._last_language = language
            self.signals.detected_language.emit(self.source, language)
        timestamp = self._item_starts.get(item_id, self._fallback_item_start or time.time())

        if event_type.endswith(".text"):
            preview = combine_qwen_preview(
                str(data.get("text") or ""), str(data.get("stash") or "")
            )
            if not preview or self._item_last_preview.get(item_id) == preview:
                return
            self._item_last_preview[item_id] = preview
            self.signals.partial.emit(self.source, timestamp, preview)
            return

        transcript = str(data.get("transcript") or "").strip()
        if not transcript:
            return
        self._item_last_preview.pop(item_id, None)
        self.signals.final.emit(self.source, timestamp, transcript)
        end_timestamp = self._item_ends.get(item_id)
        if not end_timestamp or end_timestamp <= timestamp:
            end_timestamp = max(
                timestamp + 0.2,
                self._latest_local_end,
                timestamp + min(20.0, max(0.4, len(transcript) * 0.08)),
            )
        self.signals.segment_timing.emit(self.source, timestamp, end_timestamp)

    def _receive_loop(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            while not self._receiver_stop.is_set() and not self._stop.is_set():
                try:
                    message = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if message is None:
                    break
                if isinstance(message, bytes):
                    message = message.decode("utf-8", errors="replace")
                try:
                    data = json.loads(message)
                except Exception:
                    logging.debug("Ignored non-JSON Qwen3-ASR message: %r", message)
                    continue
                if isinstance(data, dict):
                    self._handle_message(data)
        except Exception as exc:
            if not self._receiver_stop.is_set() and not self._stop.is_set():
                message = self._friendly_error(exc)
                logging.exception("Qwen3-ASR receive loop failed source=%s", self.source)
                self.signals.failed.emit(self.source, message)
        finally:
            self._session_finished.set()

    def _finish_connection(self, *, graceful: bool) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            self._flush_audio(force=True)
            if graceful and getattr(ws, "connected", True):
                self._session_finished.clear()
                self._send_json(
                    {
                        "event_id": f"event_{uuid.uuid4().hex}",
                        "type": "session.finish",
                    }
                )
                self._session_finished.wait(timeout=float(self.config.final_timeout))
        except Exception:
            logging.debug("Could not finish Qwen3-ASR session cleanly", exc_info=True)
        finally:
            self._reset_connection()

    def _reset_connection(self) -> None:
        self._receiver_stop.set()
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        receiver = self._receiver
        self._receiver = None
        if receiver is not None and receiver is not threading.current_thread():
            receiver.join(timeout=1.0)
        self._send_buffer.clear()
        self._session_ready.clear()
        self._session_finished.set()
        self._idle_deadline = None

    def _run(self) -> None:
        while not self._stop.is_set():
            timeout = 0.25
            if self._idle_deadline is not None:
                timeout = max(0.02, min(timeout, self._idle_deadline - time.monotonic()))
            try:
                kind, payload, timestamp = self.jobs.get(timeout=max(0.02, timeout))
            except queue.Empty:
                if self._idle_deadline is not None and time.monotonic() >= self._idle_deadline:
                    self._finish_connection(graceful=True)
                continue
            try:
                if kind == "stop":
                    self._stop.set()
                    self._finish_connection(graceful=True)
                    return
                if kind == "end":
                    self._latest_local_end = max(self._latest_local_end, timestamp)
                    self._flush_audio(force=True)
                    self._idle_deadline = time.monotonic() + max(
                        4.0, float(self.config.idle_close_seconds)
                    )
                    continue
                if kind != "audio" or not payload:
                    continue
                if self._ws is None:
                    try:
                        self._open_connection(timestamp)
                    except Exception as exc:
                        message = self._friendly_error(exc)
                        logging.exception("Qwen3-ASR connection failed source=%s", self.source)
                        self.signals.failed.emit(self.source, message)
                        self._reset_connection()
                        continue
                self._idle_deadline = None
                self._record_audio_span(payload, timestamp)
                self._send_buffer.extend(payload)
                self._flush_audio(force=False)
            except Exception as exc:
                message = self._friendly_error(exc)
                logging.exception("Qwen3-ASR streaming worker failed source=%s", self.source)
                self.signals.failed.emit(self.source, message)
                self._reset_connection()
            finally:
                self.jobs.task_done()
