from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

import websocket
from PySide6.QtCore import QObject, Signal


QWEN_AUDIO_STREAMING_MODEL = "qwen-audio-3.0-asr-flash-streaming"


@dataclass(frozen=True)
class QwenStreamingConfig:
    api_key: str
    workspace_id: str
    region: str = "cn-beijing"
    model: str = QWEN_AUDIO_STREAMING_MODEL
    language_hints: str = "auto"
    max_sentence_silence: int = 800
    semantic_punctuation_enabled: bool = False
    packet_ms: int = 100
    hotwords: str = ""
    proxy_url: str = ""
    connect_timeout: float = 10.0
    task_start_timeout: float = 8.0
    final_timeout: float = 8.0

    @property
    def endpoint(self) -> str:
        workspace = self.workspace_id.strip()
        if not workspace:
            raise ValueError("Qwen-Audio Streaming 缺少 Workspace ID。")
        region = (self.region or "cn-beijing").strip().lower()
        if region in {"cn-beijing", "beijing", "cn"}:
            host = f"{workspace}.cn-beijing.maas.aliyuncs.com"
        elif region in {"ap-southeast-1", "singapore", "sg", "intl"}:
            host = f"{workspace}.ap-southeast-1.maas.aliyuncs.com"
        else:
            raise ValueError(f"不支持的百炼地域：{self.region}")
        return f"wss://{host}/api-ws/v1/inference"


class QwenStreamingSignals(QObject):
    partial = Signal(str, float, str)
    final = Signal(str, float, str)
    segment_timing = Signal(str, float, float)
    status = Signal(str, str)
    failed = Signal(str, str)
    usage = Signal(str, int)


def _proxy_kwargs(proxy_url: str, endpoint_host: str) -> dict[str, Any]:
    value = (proxy_url or "").strip()
    if not value:
        # Keep the application's proxy scope authoritative instead of silently
        # inheriting unrelated HTTP(S)_PROXY environment variables.
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


def parse_language_hints(value: str) -> list[str]:
    raw = (value or "").strip().lower()
    if not raw or raw in {"auto", "自动", "自动检测", "自动识别"}:
        return []
    aliases = {
        "zh-cn": "zh",
        "zh_cn": "zh",
        "chinese": "zh",
        "中文": "zh",
        "en-us": "en",
        "en_us": "en",
        "english": "en",
        "英文": "en",
        "ja-jp": "ja",
        "ja_jp": "ja",
        "japanese": "ja",
        "日语": "ja",
        "ko-kr": "ko",
        "ko_kr": "ko",
        "korean": "ko",
        "韩语": "ko",
    }
    parts = []
    for token in raw.replace("，", ",").replace(";", ",").replace("；", ",").split(","):
        token = token.strip()
        if not token:
            continue
        token = aliases.get(token, token)
        if token not in parts:
            parts.append(token)
        if len(parts) >= 4:
            break
    return parts


def parse_hotwords(value: str) -> dict[str, int]:
    words: dict[str, int] = {}
    normalized = (value or "").replace("，", ",").replace("；", ",").replace(";", ",")
    for line in normalized.splitlines():
        for item in line.split(","):
            word = item.strip()
            if not word:
                continue
            # Qwen-Audio 3.0 supports immediate vocabulary weights 1-5 or 50.
            # A conservative weight of 5 improves names without making every item
            # an aggressive super-hotword.
            words[word] = 5
            if len(words) >= 200:
                return words
    return words


def build_run_task(config: QwenStreamingConfig, task_id: str) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "format": "pcm",
        "sample_rate": 16000,
        "semantic_punctuation_enabled": bool(config.semantic_punctuation_enabled),
        "max_sentence_silence": max(200, min(6000, int(config.max_sentence_silence))),
        "heartbeat": False,
    }
    hints = parse_language_hints(config.language_hints)
    if hints:
        parameters["language_hints"] = hints
    vocabulary = parse_hotwords(config.hotwords)
    if vocabulary:
        parameters["vocabulary"] = vocabulary
    return {
        "header": {
            "action": "run-task",
            "task_id": task_id,
            "streaming": "duplex",
        },
        "payload": {
            "task_group": "audio",
            "task": "asr",
            "function": "recognition",
            "model": config.model or QWEN_AUDIO_STREAMING_MODEL,
            "parameters": parameters,
            "input": {},
        },
    }


def build_finish_task(task_id: str) -> dict[str, Any]:
    return {
        "header": {
            "action": "finish-task",
            "task_id": task_id,
            "streaming": "duplex",
        },
        "payload": {"input": {}},
    }


def extract_sentence(message: dict[str, Any]) -> Optional[dict[str, Any]]:
    try:
        sentence = message["payload"]["output"]["sentence"]
    except (KeyError, TypeError):
        return None
    return sentence if isinstance(sentence, dict) else None


class QwenStreamingWorker:
    """Low-latency Qwen-Audio 3.0 streaming ASR for one audio source.

    The audio engine provides VAD-gated 16 kHz mono PCM. A WebSocket connection is
    kept available and a new DashScope task is opened for each local speech segment.
    Every result-generated event is forwarded immediately, so intermediate text is
    rendered in place and sentence_end=true becomes the final subtitle.
    """

    def __init__(
        self,
        source: str,
        config: QwenStreamingConfig,
        signals: QwenStreamingSignals,
    ) -> None:
        self.source = source
        self.config = config
        self.signals = signals
        self.jobs: queue.Queue[tuple[str, bytes, float]] = queue.Queue(maxsize=4096)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"QwenAudioStreaming-{source}",
            daemon=True,
        )
        self._ws: Optional[websocket.WebSocket] = None
        self._receiver: Optional[threading.Thread] = None
        self._receiver_stop = threading.Event()
        self._connection_lost = threading.Event()
        self._task_started = threading.Event()
        self._task_finished = threading.Event()
        self._task_failed = threading.Event()
        self._state_lock = threading.RLock()
        self._task_id = ""
        self._task_error = ""
        self._task_audio_origin = 0.0
        self._send_buffer = bytearray()
        self._sentence_timestamps: dict[int, float] = {}

    def start(self) -> None:
        self._thread.start()

    def feed(self, pcm: bytes, timestamp: float) -> None:
        if self._stop.is_set() or not pcm:
            return
        try:
            self.jobs.put_nowait(("audio", pcm, float(timestamp)))
        except queue.Full:
            self.signals.failed.emit(self.source, "Qwen 实时音频队列已满，当前语句已重置。")
            self._reset_task(close_connection=True)

    def end(self, timestamp: float) -> None:
        if self._stop.is_set():
            return
        try:
            self.jobs.put_nowait(("end", b"", float(timestamp)))
        except queue.Full:
            self._reset_task(close_connection=True)

    def stop(self) -> None:
        if self._stop.is_set():
            return
        try:
            self.jobs.put(("stop", b"", time.time()), timeout=0.5)
        except queue.Full:
            self._stop.set()
        self._thread.join(timeout=3.0)
        self._stop.set()
        self._close_connection()

    def _run(self) -> None:
        packet_bytes = max(1600, int(16000 * 2 * max(50, self.config.packet_ms) / 1000))
        while not self._stop.is_set():
            try:
                kind, payload, timestamp = self.jobs.get(timeout=0.25)
            except queue.Empty:
                continue
            if kind == "stop":
                if self._task_id:
                    self._finish_task(wait=True)
                break
            if kind == "audio":
                try:
                    self._ensure_task(timestamp)
                    self._send_buffer.extend(payload)
                    while len(self._send_buffer) >= packet_bytes:
                        chunk = bytes(self._send_buffer[:packet_bytes])
                        del self._send_buffer[:packet_bytes]
                        self._send_binary(chunk)
                except Exception as exc:
                    logging.exception("Qwen streaming send failed source=%s", self.source)
                    self.signals.failed.emit(self.source, str(exc))
                    self._reset_task(close_connection=True)
            elif kind == "end":
                if self._task_id:
                    self._finish_task(wait=True)
        self._close_connection()

    def _ensure_connection(self) -> None:
        with self._state_lock:
            if self._ws is not None and not self._connection_lost.is_set():
                return
        self._close_connection()
        endpoint = self.config.endpoint
        host = urlparse(endpoint).hostname or ""
        headers = [
            f"Authorization: Bearer {self.config.api_key.strip()}",
            "User-Agent: MiMo-Live-Caption/QwenAudioStreaming",
        ]
        self.signals.status.emit(self.source, "正在连接 Qwen-Audio 3.0")
        ws = websocket.create_connection(
            endpoint,
            header=headers,
            timeout=float(self.config.connect_timeout),
            enable_multithread=True,
            **_proxy_kwargs(self.config.proxy_url, host),
        )
        ws.settimeout(1.0)
        with self._state_lock:
            self._ws = ws
            self._connection_lost.clear()
            self._receiver_stop.clear()
        self._receiver = threading.Thread(
            target=self._receive_loop,
            name=f"QwenAudioReceiver-{self.source}",
            daemon=True,
        )
        self._receiver.start()
        self.signals.status.emit(self.source, "已连接，等待语音")
        logging.info(
            "Qwen-Audio streaming connected source=%s endpoint=%s model=%s proxy=%s",
            self.source,
            endpoint,
            self.config.model,
            self.config.proxy_url or "direct",
        )

    def _ensure_task(self, timestamp: float) -> None:
        if self._task_id:
            return
        if not self.config.api_key.strip():
            raise ValueError("缺少阿里云百炼 API Key。")
        self._ensure_connection()
        # Official examples use a 32-character UUID without hyphens.
        task_id = uuid.uuid4().hex
        self._task_started.clear()
        self._task_finished.clear()
        self._task_failed.clear()
        self._task_error = ""
        self._task_audio_origin = float(timestamp)
        self._send_buffer.clear()
        self._sentence_timestamps.clear()
        with self._state_lock:
            self._task_id = task_id
        self._send_json(build_run_task(self.config, task_id))
        if not self._task_started.wait(float(self.config.task_start_timeout)):
            detail = self._task_error or "等待 task-started 超时。"
            self._reset_task(close_connection=True)
            raise RuntimeError("Qwen-Audio Streaming 启动失败：" + detail)
        if self._task_failed.is_set():
            detail = self._task_error or "服务端拒绝启动任务。"
            self._reset_task(close_connection=True)
            raise RuntimeError("Qwen-Audio Streaming 启动失败：" + detail)
        self.signals.status.emit(self.source, "正在实时识别")

    def _finish_task(self, *, wait: bool) -> None:
        with self._state_lock:
            task_id = self._task_id
        if not task_id:
            return
        try:
            if self._send_buffer:
                tail = bytes(self._send_buffer)
                self._send_buffer.clear()
                if len(tail) < 1600:
                    tail += b"\x00" * (1600 - len(tail))
                self._send_binary(tail)
            self._send_json(build_finish_task(task_id))
            self.signals.status.emit(self.source, "正在定稿")
            if wait:
                finished = self._task_finished.wait(float(self.config.final_timeout))
                if self._task_failed.is_set() and self._task_error:
                    raise RuntimeError(self._task_error)
                if not finished:
                    raise RuntimeError("等待 Qwen-Audio 最终结果超时。")
        except Exception as exc:
            logging.warning("Qwen finish-task failed source=%s: %s", self.source, exc)
            if not self._stop.is_set():
                self.signals.failed.emit(self.source, str(exc))
            self._reset_task(close_connection=True)
            return
        self._reset_task(close_connection=False)
        self.signals.status.emit(self.source, "等待下一段")

    def _send_json(self, data: dict[str, Any]) -> None:
        with self._state_lock:
            ws = self._ws
        if ws is None:
            raise RuntimeError("Qwen WebSocket 尚未连接。")
        ws.send(json.dumps(data, ensure_ascii=False, separators=(",", ":")))

    def _send_binary(self, data: bytes) -> None:
        if not data:
            return
        with self._state_lock:
            ws = self._ws
        if ws is None:
            raise RuntimeError("Qwen WebSocket 尚未连接。")
        ws.send(data, opcode=websocket.ABNF.OPCODE_BINARY)

    def _receive_loop(self) -> None:
        unexpected_error = ""
        while not self._receiver_stop.is_set() and not self._stop.is_set():
            with self._state_lock:
                ws = self._ws
            if ws is None:
                break
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as exc:
                if not self._receiver_stop.is_set() and not self._stop.is_set():
                    unexpected_error = str(exc)
                break
            if raw in (None, b"", ""):
                break
            if isinstance(raw, bytes):
                try:
                    raw = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue
            try:
                message = json.loads(raw)
            except Exception:
                logging.debug("Ignored non-JSON Qwen message source=%s", self.source)
                continue
            self._handle_message(message)
        self._connection_lost.set()
        active_task = bool(self._task_id and not self._task_finished.is_set())
        if active_task:
            self._task_error = unexpected_error or "Qwen WebSocket 在任务完成前已断开。"
            self._task_failed.set()
        self._task_started.set()
        self._task_finished.set()
        if unexpected_error:
            if active_task:
                logging.warning(
                    "Qwen receiver ended during task source=%s error=%s",
                    self.source,
                    unexpected_error,
                )
                if not self._stop.is_set():
                    self.signals.failed.emit(self.source, unexpected_error)
            else:
                # The service closes an idle reusable connection after a timeout.
                # This is normal; the next speech segment reconnects automatically.
                logging.info(
                    "Qwen idle WebSocket closed source=%s detail=%s",
                    self.source,
                    unexpected_error,
                )

    def _handle_message(self, message: dict[str, Any]) -> None:
        header = message.get("header") if isinstance(message, dict) else None
        if not isinstance(header, dict):
            return
        event = str(header.get("event") or "")
        task_id = str(header.get("task_id") or "")
        with self._state_lock:
            active_task_id = self._task_id
        if task_id and active_task_id and task_id != active_task_id:
            return
        if event == "task-started":
            self._task_started.set()
            return
        if event == "result-generated":
            sentence = extract_sentence(message)
            if not sentence or bool(sentence.get("heartbeat")):
                return
            text = str(sentence.get("text") or "").strip()
            if not text:
                return
            begin_ms = int(sentence.get("begin_time") or 0)
            end_value = sentence.get("end_time")
            end_ms = int(end_value or begin_ms)
            try:
                sentence_id = int(sentence.get("sentence_id") or 0)
            except (TypeError, ValueError):
                sentence_id = 0
            candidate_timestamp = (
                self._task_audio_origin + max(0, begin_ms) / 1000.0
            )
            if sentence_id > 0:
                timestamp = self._sentence_timestamps.setdefault(
                    sentence_id, candidate_timestamp
                )
            else:
                timestamp = candidate_timestamp
            end_timestamp = self._task_audio_origin + max(begin_ms, end_ms) / 1000.0
            is_final = bool(sentence.get("sentence_end"))
            if is_final:
                self.signals.final.emit(self.source, timestamp, text)
                if end_timestamp > timestamp:
                    self.signals.segment_timing.emit(
                        self.source, timestamp, end_timestamp
                    )
                usage = message.get("payload", {}).get("usage")
                if isinstance(usage, dict) and usage.get("duration") is not None:
                    try:
                        self.signals.usage.emit(self.source, int(usage["duration"]))
                    except (TypeError, ValueError):
                        pass
            else:
                self.signals.partial.emit(self.source, timestamp, text)
            return
        if event == "task-finished":
            self._task_finished.set()
            return
        if event == "task-failed":
            code = str(header.get("error_code") or "TASK_FAILED")
            detail = str(header.get("error_message") or "未知错误")
            self._task_error = f"{code}: {detail}"
            self._task_failed.set()
            self._task_started.set()
            self._task_finished.set()
            self.signals.failed.emit(self.source, self._task_error)

    def _reset_task(self, *, close_connection: bool) -> None:
        with self._state_lock:
            self._task_id = ""
        self._task_audio_origin = 0.0
        self._send_buffer.clear()
        self._sentence_timestamps.clear()
        self._task_started.clear()
        self._task_finished.clear()
        self._task_failed.clear()
        self._task_error = ""
        if close_connection:
            self._close_connection()

    def _close_connection(self) -> None:
        self._receiver_stop.set()
        with self._state_lock:
            ws = self._ws
            self._ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        receiver = self._receiver
        if receiver is not None and receiver is not threading.current_thread():
            receiver.join(timeout=1.0)
        self._receiver = None
        self._connection_lost.set()
