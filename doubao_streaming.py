from __future__ import annotations

import gzip
import json
import logging
import queue
import struct
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse, urlsplit, urlunsplit

import websocket
from PySide6.QtCore import QObject, Signal


@dataclass(frozen=True)
class DoubaoStreamingConfig:
    app_id: str = ""
    access_token: str = ""
    api_key: str = ""
    endpoint: str = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
    resource_id: str = "volc.seedasr.sauc.duration"
    enable_nonstream: bool = True
    language: str = "auto"
    end_window_ms: int = 800
    packet_ms: int = 200
    hotwords: str = ""
    proxy_url: str = ""
    connect_timeout: float = 8.0
    final_timeout: float = 5.0
    idle_close_seconds: float = 6.0


class DoubaoServerError(RuntimeError):
    def __init__(self, code: int, detail: str) -> None:
        self.code = int(code)
        self.detail = detail
        super().__init__(f"豆包流式 ASR 错误 {self.code}: {self.detail}")


class DoubaoStreamingSignals(QObject):
    partial = Signal(str, float, str)
    final = Signal(str, float, str)
    segment_timing = Signal(str, float, float)
    status = Signal(str, str)
    failed = Signal(str, str)
    logid = Signal(str, str)


def _build_header(
    message_type: int,
    flags: int,
    serialization: int,
    compression: int,
) -> bytes:
    return bytes(
        [
            0x11,  # protocol v1 + 4-byte base header
            ((message_type & 0x0F) << 4) | (flags & 0x0F),
            ((serialization & 0x0F) << 4) | (compression & 0x0F),
            0x00,
        ]
    )


def build_full_request(payload: dict[str, Any]) -> bytes:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(raw)
    return _build_header(0x01, 0x00, 0x01, 0x01) + struct.pack(">I", len(compressed)) + compressed


def build_audio_request(pcm: bytes, *, last: bool = False) -> bytes:
    compressed = gzip.compress(pcm)
    flags = 0x02 if last else 0x00
    return _build_header(0x02, flags, 0x00, 0x01) + struct.pack(">I", len(compressed)) + compressed


def parse_server_frame(frame: bytes) -> tuple[dict[str, Any], bool]:
    if len(frame) < 4:
        raise RuntimeError("豆包返回了过短的二进制消息。")
    header_size = (frame[0] & 0x0F) * 4
    message_type = (frame[1] >> 4) & 0x0F
    flags = frame[1] & 0x0F
    serialization = (frame[2] >> 4) & 0x0F
    compression = frame[2] & 0x0F
    offset = header_size

    if message_type == 0x0F:
        if len(frame) < offset + 8:
            raise RuntimeError("豆包返回了损坏的错误消息。")
        code = struct.unpack(">I", frame[offset : offset + 4])[0]
        size = struct.unpack(">I", frame[offset + 4 : offset + 8])[0]
        body = frame[offset + 8 : offset + 8 + size]
        try:
            detail = body.decode("utf-8", errors="replace")
        except Exception:
            detail = repr(body)
        raise DoubaoServerError(code, detail)

    if message_type != 0x09:
        return {}, bool(flags & 0x02)

    if flags & 0x01:
        if len(frame) < offset + 4:
            raise RuntimeError("豆包响应缺少 sequence。")
        offset += 4
    if len(frame) < offset + 4:
        raise RuntimeError("豆包响应缺少 payload size。")
    size = struct.unpack(">I", frame[offset : offset + 4])[0]
    payload = frame[offset + 4 : offset + 4 + size]
    if compression == 0x01:
        payload = gzip.decompress(payload)
    if serialization == 0x01 and payload:
        data = json.loads(payload.decode("utf-8"))
    else:
        data = {}
    return data if isinstance(data, dict) else {}, bool(flags & 0x02)


def _proxy_kwargs(proxy_url: str, endpoint_host: str) -> dict[str, Any]:
    value = (proxy_url or "").strip()
    if not value:
        # websocket-client otherwise consults environment proxy variables. The app's
        # network page is the single source of truth, so explicitly bypass them here.
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
    auth = None
    if parsed.username:
        auth = (parsed.username, parsed.password or "")
    result: dict[str, Any] = {
        "http_proxy_host": parsed.hostname,
        "http_proxy_port": parsed.port,
        "proxy_type": proxy_type,
    }
    if auth:
        result["http_proxy_auth"] = auth
    return result


class DoubaoStreamingWorker:
    """One VAD-gated streaming session at a time for a single audio source."""

    def __init__(
        self,
        source: str,
        config: DoubaoStreamingConfig,
        signals: DoubaoStreamingSignals,
    ) -> None:
        self.source = source
        self.config = config
        self.signals = signals
        self.jobs: queue.Queue[tuple[str, bytes, float]] = queue.Queue(maxsize=2048)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"DoubaoStreaming-{source}",
            daemon=True,
        )
        self._ws: Optional[websocket.WebSocket] = None
        self._receiver: Optional[threading.Thread] = None
        self._receiver_stop = threading.Event()
        self._server_last = threading.Event()
        self._definite_received = threading.Event()
        self._pending_finalized = threading.Event()
        self._pending_finalized.set()
        self._state_lock = threading.RLock()
        self._session_started_at = 0.0
        self._send_buffer = bytearray()
        self._utterance_timestamps: dict[str, float] = {}
        self._utterance_states: dict[str, tuple[str, bool]] = {}
        self._anonymous_generation = 0
        self._anonymous_current_key: Optional[str] = None
        self._last_anonymous_final: Optional[tuple[str, int, int, str]] = None
        self._last_utterance_end_ms = 0
        self._result_revision = 0
        self._last_assigned_timestamp = 0.0
        self._last_result_text = ""
        self._last_result_timestamp = 0.0
        self._last_result_finalized = False
        self._logid = ""
        self._idle_deadline: Optional[float] = None

    def start(self) -> None:
        self._thread.start()

    def feed(self, pcm: bytes, timestamp: float) -> None:
        if self._stop.is_set() or not pcm:
            return
        try:
            self.jobs.put_nowait(("audio", pcm, float(timestamp)))
        except queue.Full:
            self.signals.failed.emit(self.source, "豆包实时音频队列已满；当前语句已重置。")
            self._reset_session()

    def end(self, timestamp: float) -> None:
        if self._stop.is_set():
            return
        try:
            self.jobs.put_nowait(("end", b"", float(timestamp)))
        except queue.Full:
            self._reset_session()

    def stop(self) -> None:
        if self._stop.is_set():
            return
        try:
            self.jobs.put(("stop", b"", time.time()), timeout=0.5)
        except queue.Full:
            self._stop.set()
        self._thread.join(timeout=2.5)
        if self._thread.is_alive():
            self._stop.set()
        self._reset_session()

    def _language_mode(self) -> tuple[str, bool]:
        language = str(self.config.language or "auto").strip() or "auto"
        # The optimized bidirectional endpoint supports Chinese and English. Other
        # explicit languages, plus automatic multilingual detection, require the
        # streaming-input endpoint, which returns accumulated snapshots rather than
        # true token-by-token revisions.
        return language, language not in {"zh-CN", "en-US"}

    def _resolved_endpoint(self) -> str:
        """Select the protocol endpoint required by the configured language mode."""
        language, multilingual_single_pass = self._language_mode()
        endpoint = self.config.endpoint.strip()
        if not endpoint:
            endpoint = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
        parsed = urlsplit(endpoint)
        known_suffixes = ("/bigmodel", "/bigmodel_async", "/bigmodel_nostream")
        if parsed.hostname == "openspeech.bytedance.com" or parsed.path.endswith(known_suffixes):
            desired = (
                "/api/v3/sauc/bigmodel_nostream"
                if multilingual_single_pass
                else "/api/v3/sauc/bigmodel_async"
            )
            endpoint = urlunsplit((parsed.scheme or "wss", parsed.netloc, desired, parsed.query, parsed.fragment))
        return endpoint

    def _request_payload(self) -> dict[str, Any]:
        language, multilingual_single_pass = self._language_mode()
        # The WebSocket path determines async vs nostream mode. The documented
        # request model name remains ``bigmodel`` in both modes. Explicit language
        # belongs to the top-level ``audio`` object, while enable_auto_lang is a
        # request option. Reusing the async endpoint or putting language under
        # request silently leaves the default Chinese/English recognizer active.
        request: dict[str, Any] = {
            "model_name": "bigmodel",
            "enable_nonstream": (
                False
                if multilingual_single_pass
                else bool(self.config.enable_nonstream)
            ),
            "ssd_version": "200",
            "enable_itn": True,
            "enable_punc": True,
            "enable_ddc": False,
            # bigmodel_nostream may resend the complete accumulated utterance list
            # after every later packet. Consume only its top-level full snapshot to
            # avoid appending the same Japanese paragraph dozens of times.
            "show_utterances": False if multilingual_single_pass else True,
            "result_type": "full" if multilingual_single_pass else "single",
            "enable_accelerate_text": False,
        }
        if multilingual_single_pass:
            if language == "auto":
                request["enable_auto_lang"] = True
        else:
            request["end_window_size"] = max(200, int(self.config.end_window_ms))
            request["force_to_speech_time"] = 800

        words = [
            item.strip()
            for item in self.config.hotwords.replace("，", ",").replace("\n", ",").split(",")
            if item.strip()
        ][:100]
        if words:
            # Volcengine expects context as a JSON *string* at request level.
            request["context"] = json.dumps(
                {"hotwords": [{"word": word} for word in words]},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        audio: dict[str, Any] = {
            "format": "pcm",
            "codec": "raw",
            "rate": 16000,
            "bits": 16,
            "channel": 1,
        }
        if multilingual_single_pass and language != "auto":
            audio["language"] = language
        return {
            "user": {
                "uid": f"mimo-live-caption-{self.source}-{uuid.uuid4().hex[:12]}",
                "platform": "Windows",
                "app_version": "1.0.18",
            },
            "audio": audio,
            "request": request,
        }

    def _open_session(self, timestamp: float) -> None:
        endpoint = self._resolved_endpoint()
        if not endpoint.startswith("wss://"):
            raise RuntimeError("豆包流式地址必须以 wss:// 开头。")
        connect_id = str(uuid.uuid4())
        headers = [
            f"X-Api-Resource-Id: {self.config.resource_id}",
            f"X-Api-Connect-Id: {connect_id}",
        ]
        if self.config.api_key.strip():
            # New Volcengine speech console credential.
            headers.append(f"X-Api-Key: {self.config.api_key.strip()}")
        else:
            # Legacy console credential pair.
            headers.extend(
                [
                    f"X-Api-App-Key: {self.config.app_id.strip()}",
                    f"X-Api-Access-Key: {self.config.access_token.strip()}",
                ]
            )
        host = urlparse(endpoint).hostname or "openspeech.bytedance.com"
        kwargs = _proxy_kwargs(self.config.proxy_url, host)
        self.signals.status.emit(self.source, "正在连接豆包流式 ASR")
        ws = websocket.create_connection(
            endpoint,
            header=headers,
            timeout=self.config.connect_timeout,
            enable_multithread=True,
            **kwargs,
        )
        ws.settimeout(1.0)
        self._ws = ws
        self._session_started_at = timestamp
        self._send_buffer.clear()
        with self._state_lock:
            self._utterance_timestamps.clear()
            self._utterance_states.clear()
            self._anonymous_generation = 0
            self._anonymous_current_key = None
            self._last_anonymous_final = None
            self._last_utterance_end_ms = 0
            self._result_revision = 0
            self._last_assigned_timestamp = timestamp - 0.001
            self._pending_finalized.set()
        self._last_result_text = ""
        self._last_result_timestamp = timestamp
        self._last_result_finalized = False
        self._idle_deadline = None
        self._server_last.clear()
        self._definite_received.clear()
        self._receiver_stop.clear()
        try:
            response_headers = ws.getheaders() or {}
            if isinstance(response_headers, dict):
                self._logid = str(
                    response_headers.get("x-tt-logid")
                    or response_headers.get("X-Tt-Logid")
                    or ""
                )
            else:
                self._logid = ""
        except Exception:
            self._logid = ""
        if self._logid:
            self.signals.logid.emit(self.source, self._logid)
        request_payload = self._request_payload()
        request_options = request_payload.get("request", {})
        audio_options = request_payload.get("audio", {})
        logging.info(
            "Doubao request source=%s endpoint=%s model_name=%s language=%s auto_lang=%s nonstream=%s result_type=%s",
            self.source,
            endpoint,
            request_options.get("model_name"),
            audio_options.get("language", ""),
            bool(request_options.get("enable_auto_lang", False)),
            bool(request_options.get("enable_nonstream", False)),
            request_options.get("result_type", ""),
        )
        ws.send_binary(build_full_request(request_payload))
        self._receiver = threading.Thread(
            target=self._receive_loop,
            name=f"DoubaoReceive-{self.source}",
            daemon=True,
        )
        self._receiver.start()
        self.signals.status.emit(self.source, "豆包流式 ASR 已连接")

    def _receive_loop(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            while not self._receiver_stop.is_set() and not self._stop.is_set():
                try:
                    frame = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if frame is None:
                    break
                if isinstance(frame, str):
                    try:
                        payload = json.loads(frame)
                    except Exception:
                        continue
                    is_last = False
                else:
                    payload, is_last = parse_server_frame(bytes(frame))
                if payload:
                    self._handle_payload(payload)
                if is_last:
                    self._server_last.set()
                    break
        except DoubaoServerError as exc:
            if exc.code == 45000081:
                # The service closes an idle stream after waiting eight seconds for
                # another packet. This is a normal session boundary, not a user-facing
                # recognition failure. Preserve the latest partial and reconnect on
                # the next utterance.
                logging.info(
                    "Doubao idle packet timeout source=%s logid=%s detail=%s",
                    self.source, self._logid, exc.detail,
                )
                self._promote_pending_to_final()
                self.signals.status.emit(self.source, "空闲会话已正常结束")
            elif not self._receiver_stop.is_set() and not self._stop.is_set():
                suffix = f"（logid: {self._logid}）" if self._logid else ""
                self.signals.failed.emit(self.source, f"{exc}{suffix}")
        except Exception as exc:
            if not self._receiver_stop.is_set() and not self._stop.is_set():
                suffix = f"（logid: {self._logid}）" if self._logid else ""
                self.signals.failed.emit(self.source, f"{exc}{suffix}")
        finally:
            self._server_last.set()

    @staticmethod
    def _result_text_key(text: str) -> str:
        return " ".join(str(text or "").casefold().split())

    def _refresh_pending_event_locked(self) -> None:
        if any(not definite for _, definite in self._utterance_states.values()):
            self._pending_finalized.clear()
        else:
            self._pending_finalized.set()

    def _anonymous_utterance_key_locked(
        self, text: str, definite: bool, start_ms: int, end_ms: int
    ) -> tuple[str, bool]:
        """Return a stable key for result_type=single responses without an id.

        Some partial packets omit ``start_time`` (or report zero) and the final
        packet later supplies the real start time. Using start time as the row id
        therefore turns one utterance into two subtitle rows. In single-result
        mode there is only one current non-definite utterance, so keep a local slot
        until that slot becomes definite.
        """
        current = self._anonymous_current_key
        if current is not None:
            state = self._utterance_states.get(current)
            if state is not None and not state[1]:
                return current, False

        pending = [
            key
            for key, (_, is_definite) in self._utterance_states.items()
            if not is_definite
        ]
        if len(pending) == 1:
            self._anonymous_current_key = pending[0]
            return pending[0], False

        text_key = self._result_text_key(text)
        last = self._last_anonymous_final
        if definite and last is not None:
            last_text, last_start, last_end, last_key = last
            same_times = (start_ms == last_start and end_ms == last_end)
            if text_key == last_text and same_times:
                return last_key, True

        self._anonymous_generation += 1
        key = f"anonymous:{self._anonymous_generation}"
        self._anonymous_current_key = key
        return key, False

    def _handle_multilingual_results(self, results: list[dict[str, Any]]) -> None:
        """Treat nostream responses as one replaceable full-session snapshot.

        The multilingual endpoint is not a bidirectional token stream. Once it starts
        responding, later packets can contain the same full prefix again (sometimes
        with slightly changed timings). Mapping every returned utterance to a new row
        caused the repeated Japanese paragraphs seen in 1.0.17. Keep one snapshot per
        session and finalize it exactly once when the negative/last packet completes.
        """
        candidates: list[str] = []
        for result in results:
            text = str(result.get("text") or "").strip()
            if text:
                candidates.append(text)
                continue
            utterances = result.get("utterances")
            if isinstance(utterances, list):
                pieces = [
                    str(item.get("text") or "").strip()
                    for item in utterances
                    if isinstance(item, dict) and str(item.get("text") or "").strip()
                ]
                if pieces:
                    candidates.append("".join(pieces))
        if not candidates:
            return

        # A full snapshot is normally the longest candidate in a packet. Selecting it
        # is safer than concatenating multiple representations of the same prefix.
        text = max(candidates, key=lambda value: len(value.strip())).strip()
        key = self._result_text_key(text)
        previous_key = self._result_text_key(self._last_result_text)
        if not key or key == previous_key:
            return
        # Ignore a transient shorter regression from an out-of-order response.
        if previous_key and previous_key.startswith(key) and len(key) < len(previous_key):
            return
        with self._state_lock:
            self._last_result_text = text
            self._last_result_timestamp = self._session_started_at or time.time()
            self._last_result_finalized = False
            self._result_revision += 1
            timestamp = self._last_result_timestamp
        self.signals.partial.emit(self.source, timestamp, text)

    def _handle_payload(self, payload: dict[str, Any]) -> None:
        raw_result = payload.get("result")
        if isinstance(raw_result, list):
            results = [item for item in raw_result if isinstance(item, dict)]
        elif isinstance(raw_result, dict):
            results = [raw_result]
        else:
            results = []
        if not results:
            return

        _, multilingual_single_pass = self._language_mode()
        if multilingual_single_pass:
            self._handle_multilingual_results(results)
            return

        result = results[0]
        utterances = result.get("utterances")
        if isinstance(utterances, list) and utterances:
            emissions: list[tuple[bool, float, str, int]] = []
            timing_emissions: list[tuple[float, float]] = []
            with self._state_lock:
                for item in utterances:
                    if not isinstance(item, dict):
                        continue
                    text = str(item.get("text") or "").strip()
                    if not text:
                        continue
                    definite = bool(item.get("definite", False))
                    start_ms = int(item.get("start_time") or 0)
                    end_ms = int(item.get("end_time") or 0)
                    provider_id = item.get("utterance_id") or item.get("id")
                    duplicate_frame = False
                    if provider_id is not None:
                        utterance_key = f"provider:{provider_id}"
                    else:
                        utterance_key, duplicate_frame = self._anonymous_utterance_key_locked(
                            text, definite, start_ms, end_ms
                        )

                    if utterance_key not in self._utterance_timestamps:
                        fallback_ms = max(0, self._last_utterance_end_ms)
                        effective_start_ms = start_ms if start_ms > 0 else fallback_ms
                        candidate_timestamp = (
                            self._session_started_at + effective_start_ms / 1000.0
                        )
                        # Some responses omit both start/end times. Keep subtitle
                        # identities unique and ordered even in that fallback case.
                        if candidate_timestamp <= self._last_assigned_timestamp:
                            candidate_timestamp = self._last_assigned_timestamp + 0.001
                        self._last_assigned_timestamp = candidate_timestamp
                        self._utterance_timestamps[utterance_key] = candidate_timestamp

                    state = (text, definite)
                    if duplicate_frame or self._utterance_states.get(utterance_key) == state:
                        continue
                    self._utterance_states[utterance_key] = state
                    self._result_revision += 1
                    timestamp = self._utterance_timestamps[utterance_key]
                    if end_ms > start_ms:
                        timing_emissions.append(
                            (timestamp, self._session_started_at + end_ms / 1000.0)
                        )
                    emissions.append((definite, timestamp, text, end_ms))
                    if definite:
                        self._definite_received.set()
                        self._last_utterance_end_ms = max(
                            self._last_utterance_end_ms, end_ms
                        )
                        if provider_id is None:
                            self._last_anonymous_final = (
                                self._result_text_key(text),
                                start_ms,
                                end_ms,
                                utterance_key,
                            )
                            if self._anonymous_current_key == utterance_key:
                                self._anonymous_current_key = None
                self._refresh_pending_event_locked()

            for timestamp, end_timestamp in timing_emissions:
                self.signals.segment_timing.emit(
                    self.source, timestamp, end_timestamp
                )
            for definite, timestamp, text, _ in emissions:
                if definite:
                    self.signals.final.emit(self.source, timestamp, text)
                else:
                    self.signals.partial.emit(self.source, timestamp, text)
            return

        text = str(result.get("text") or "").strip()
        if text and text != self._last_result_text:
            with self._state_lock:
                self._last_result_text = text
                self._last_result_timestamp = self._session_started_at or time.time()
                self._last_result_finalized = False
                self._result_revision += 1
                timestamp = self._last_result_timestamp
            self.signals.partial.emit(self.source, timestamp, text)

    def _promote_pending_to_final(self) -> None:
        """Use the latest partial as a safe fallback if the server closes without definite."""
        emissions: list[tuple[float, str]] = []
        with self._state_lock:
            for utterance_key, state in list(self._utterance_states.items()):
                text, definite = state
                if definite or not text.strip():
                    continue
                timestamp = self._utterance_timestamps.get(
                    utterance_key, self._session_started_at or time.time()
                )
                self._utterance_states[utterance_key] = (text, True)
                emissions.append((timestamp, text))
            self._anonymous_current_key = None
            self._refresh_pending_event_locked()
        for timestamp, text in emissions:
            self.signals.final.emit(self.source, timestamp, text)
        if (
            not emissions
            and self._last_result_text.strip()
            and not self._last_result_finalized
        ):
            self._last_result_finalized = True
            self.signals.final.emit(
                self.source,
                self._last_result_timestamp or self._session_started_at or time.time(),
                self._last_result_text,
            )

    def _send_audio_packet(self, pcm: bytes, *, last: bool = False) -> None:
        ws = self._ws
        if ws is None:
            return
        ws.send_binary(build_audio_request(pcm, last=last))

    def _finish_session(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            remaining = bytes(self._send_buffer)
            self._send_buffer.clear()
            with self._state_lock:
                pending_before = any(
                    not definite for _, definite in self._utterance_states.values()
                )
                revision_before = self._result_revision
            _, multilingual_single_pass = self._language_mode()
            self.signals.status.emit(
                self.source,
                "等待多语种识别结果"
                if multilingual_single_pass
                else "等待当前分句二遍定稿",
            )
            self._send_audio_packet(remaining, last=True)

            # Do not use a session-wide "any definite received" flag here. A long
            # stream can already contain many finalized sentences while its newest
            # sentence is still partial. Closing as soon as an old definite exists
            # truncates that newest sentence and leaves a duplicate provisional row.
            started = time.monotonic()
            _, multilingual_single_pass = self._language_mode()
            deadline = started + max(
                10.0 if multilingual_single_pass else 0.5,
                self.config.final_timeout,
            )
            last_seen_revision = revision_before
            last_revision_change = started
            while time.monotonic() < deadline:
                if self._server_last.wait(timeout=0.05):
                    break
                with self._state_lock:
                    pending_now = any(
                        not definite for _, definite in self._utterance_states.values()
                    )
                    revision_now = self._result_revision
                now = time.monotonic()
                elapsed = now - started
                if revision_now != last_seen_revision:
                    last_seen_revision = revision_now
                    last_revision_change = now
                if multilingual_single_pass:
                    # nostream often returns only after the negative/last packet.
                    # Never close after the async mode's old one-second grace period;
                    # wait until a result arrives and stays unchanged briefly.
                    if (
                        revision_now > revision_before
                        and now - last_revision_change >= 0.6
                    ):
                        break
                    continue
                if pending_before and not pending_now:
                    break
                if (
                    not pending_before
                    and not pending_now
                    and elapsed >= 0.45
                    and (revision_now > revision_before or elapsed >= 1.0)
                ):
                    break
            self._promote_pending_to_final()
        except DoubaoServerError as exc:
            if exc.code == 45000081:
                self._promote_pending_to_final()
            elif not self._stop.is_set():
                self.signals.failed.emit(self.source, str(exc))
        except Exception as exc:
            if not self._stop.is_set():
                self.signals.failed.emit(self.source, str(exc))
        finally:
            self._reset_session()

    def _reset_session(self) -> None:
        self._receiver_stop.set()
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        receiver, self._receiver = self._receiver, None
        if receiver is not None and receiver is not threading.current_thread():
            receiver.join(timeout=0.8)
        self._send_buffer.clear()
        self._server_last.set()
        self._session_started_at = 0.0
        self._idle_deadline = None

    def _run(self) -> None:
        packet_bytes = max(3200, int(16000 * 2 * max(100, self.config.packet_ms) / 1000))
        while True:
            timeout = 0.25
            if self._idle_deadline is not None:
                timeout = max(0.02, min(timeout, self._idle_deadline - time.monotonic()))
            try:
                kind, data, timestamp = self.jobs.get(timeout=timeout)
            except queue.Empty:
                if self._stop.is_set():
                    break
                if (
                    self._ws is not None
                    and self._idle_deadline is not None
                    and time.monotonic() >= self._idle_deadline
                ):
                    # Keep the socket warm across ordinary sentence pauses, then
                    # close after a longer idle period to avoid zombie connections.
                    self._finish_session()
                continue
            try:
                if kind == "stop":
                    if self._ws is not None:
                        self._finish_session()
                    self._stop.set()
                    break
                if kind == "end":
                    if self._ws is not None:
                        _, multilingual_single_pass = self._language_mode()
                        if multilingual_single_pass:
                            # bigmodel_nostream returns short-utterance results after
                            # the last packet. End each VAD segment explicitly and
                            # open a fresh session next time so auto language detection
                            # is rerun when the user switches videos/languages.
                            self._finish_session()
                        else:
                            # Async Chinese mode keeps the socket warm across normal
                            # sentence pauses to preserve provider utterance identity.
                            remaining = bytes(self._send_buffer)
                            self._send_buffer.clear()
                            if remaining:
                                try:
                                    self._send_audio_packet(remaining, last=False)
                                except Exception as exc:
                                    self.signals.failed.emit(self.source, str(exc))
                                    self._reset_session()
                                    continue
                            self._idle_deadline = (
                                time.monotonic()
                                + max(1.0, min(6.0, self.config.idle_close_seconds))
                            )
                            self.signals.status.emit(
                                self.source, "分句完成，连接保持等待下一句"
                            )
                    continue
                if kind != "audio" or not data:
                    continue
                self._idle_deadline = None
                if self._ws is not None and self._server_last.is_set():
                    # The receive thread may have observed a server-side idle close.
                    # Reopen before accepting new PCM so the first packet is not lost.
                    self._reset_session()
                if self._ws is None:
                    try:
                        self._open_session(timestamp)
                    except Exception as exc:
                        self.signals.failed.emit(self.source, str(exc))
                        self._reset_session()
                        continue
                self._send_buffer.extend(data)
                while len(self._send_buffer) >= packet_bytes:
                    packet = bytes(self._send_buffer[:packet_bytes])
                    del self._send_buffer[:packet_bytes]
                    try:
                        self._send_audio_packet(packet)
                    except Exception as exc:
                        self.signals.failed.emit(self.source, str(exc))
                        self._reset_session()
                        break
            finally:
                self.jobs.task_done()
        self._reset_session()
