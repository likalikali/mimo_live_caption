from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QObject, Signal


@dataclass(frozen=True)
class GoogleSpeechStreamingConfig:
    project_id: str = ""
    credentials_path: str = ""
    location: str = "us"
    model: str = "chirp_3"
    language_code: str = "ja-JP"
    sample_rate_hertz: int = 16_000
    proxy_url: str = ""
    final_timeout: float = 12.0


class GoogleSpeechStreamingSignals(QObject):
    partial = Signal(str, float, str)
    final = Signal(str, float, str)
    segment_timing = Signal(str, float, float)
    status = Signal(str, str)
    failed = Signal(str, str)


def _normalize_proxy_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = "http://" + value
    return value


def _load_google_modules() -> tuple[Any, Any, Any, Any, Any]:
    try:
        import google.auth
        from google.api_core.client_options import ClientOptions
        from google.cloud import speech_v2
        from google.cloud.speech_v2.types import cloud_speech
        from google.oauth2 import service_account
    except Exception as exc:  # pragma: no cover - depends on optional runtime package
        raise RuntimeError(
            "缺少 Google Speech-to-Text 依赖。请重新运行 run.bat，或执行 "
            "uv pip install google-cloud-speech。"
        ) from exc
    return google.auth, ClientOptions, speech_v2, cloud_speech, service_account


def _read_project_from_json(path: str) -> str:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("project_id") or "").strip()


def resolve_google_credentials(
    config: GoogleSpeechStreamingConfig,
) -> tuple[Any, str]:
    google_auth, _, _, _, service_account = _load_google_modules()
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    credentials_path = str(config.credentials_path or "").strip()
    configured_project = str(config.project_id or "").strip()

    if credentials_path:
        path = Path(credentials_path).expanduser()
        if not path.is_file():
            raise RuntimeError(f"Google 凭据 JSON 不存在：{path}")
        credentials = service_account.Credentials.from_service_account_file(
            str(path), scopes=scopes
        )
        project_id = configured_project or _read_project_from_json(str(path))
    else:
        try:
            credentials, default_project = google_auth.default(scopes=scopes)
        except Exception as exc:
            raise RuntimeError(
                "没有找到 Google Application Default Credentials。请在设置中选择服务账号 JSON，"
                "或先运行 gcloud auth application-default login。"
            ) from exc
        project_id = configured_project or str(default_project or "").strip()

    if not project_id:
        raise RuntimeError("Google Speech-to-Text 需要 Google Cloud Project ID。")
    # User ADC often needs a quota project. Service-account credentials already
    # bill against the project named by the recognizer; forcing a quota project on
    # them can unnecessarily require serviceusage.services.use.
    if not isinstance(credentials, service_account.Credentials) and not getattr(
        credentials, "quota_project_id", None
    ):
        try:
            credentials = credentials.with_quota_project(project_id)
        except Exception:
            pass
    return credentials, project_id


def google_speech_config_status(
    config: GoogleSpeechStreamingConfig,
) -> tuple[bool, str, str]:
    """Validate local package/credentials without consuming recognition quota."""
    try:
        _load_google_modules()
        _, project_id = resolve_google_credentials(config)
        location = str(config.location or "us").strip().lower()
        if location not in {"us", "eu"}:
            return False, "Chirp 3 流式日语目前请选择 us 或 eu 区域。", ""
        if str(config.model or "chirp_3") != "chirp_3":
            return False, "当前日语实时模式固定使用 chirp_3。", ""
        return True, "Google Speech-to-Text V2 凭据可用。", project_id
    except Exception as exc:
        return False, str(exc), ""


def _duration_seconds(value: Any) -> float:
    if value is None:
        return 0.0
    seconds = float(getattr(value, "seconds", 0) or 0)
    nanos = float(getattr(value, "nanos", 0) or 0)
    return max(0.0, seconds + nanos / 1_000_000_000.0)


def _join_result_parts(parts: list[str], language_code: str) -> str:
    cleaned = [part.strip() for part in parts if part and part.strip()]
    if not cleaned:
        return ""
    if language_code.startswith(("ja", "zh", "ko")):
        return "".join(cleaned)
    output = cleaned[0]
    for part in cleaned[1:]:
        if output and part and output[-1].isalnum() and part[0].isalnum():
            output += " " + part
        else:
            output += part
    return output


def _friendly_streaming_error(exc: Exception, config: GoogleSpeechStreamingConfig) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    lowered = text.lower()
    if (
        "failed to connect to all addresses" in lowered
        or "tcp handshaker shutdown" in lowered
        or "statuscode.unavailable" in lowered
    ):
        proxy = _normalize_proxy_url(config.proxy_url)
        if proxy:
            return (
                "无法通过已配置代理建立 Google Speech gRPC 连接。请确认代理地址是 "
                "HTTP/混合端口并支持 CONNECT，例如 http://127.0.0.1:20800；"
                "修改代理后请停止并重新开始识别。原始错误：" + text
            )
        return (
            "Google Speech gRPC 当前正在直连，网络握手失败。请到“设置 → 网络与代理”"
            "启用自定义代理，并勾选“语音识别 API（含 Google Speech gRPC）”；"
            "代理需使用 HTTP/混合端口。原始错误：" + text
        )
    return text


class GoogleSpeechStreamingWorker:
    """One bidirectional StreamingRecognize worker for a single audio source."""

    _END = object()

    def __init__(
        self,
        source: str,
        config: GoogleSpeechStreamingConfig,
        signals: GoogleSpeechStreamingSignals,
    ) -> None:
        self.source = source
        self.config = config
        self.signals = signals
        self.jobs: queue.Queue[tuple[str, bytes, float]] = queue.Queue(maxsize=2048)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"GoogleSpeechStreaming-{source}",
            daemon=True,
        )
        self._session_thread: Optional[threading.Thread] = None
        self._session_audio: Optional[queue.Queue[Any]] = None
        self._session_done = threading.Event()
        self._session_started_at = 0.0
        self._active_call: Any = None
        self._client: Any = None
        self._project_id = ""
        self._lock = threading.RLock()

    def start(self) -> None:
        self._thread.start()

    def feed(self, pcm: bytes, timestamp: float) -> None:
        if self._stop.is_set() or not pcm:
            return
        try:
            self.jobs.put_nowait(("audio", bytes(pcm), float(timestamp)))
        except queue.Full:
            self.signals.failed.emit(
                self.source, "Google 日语实时音频队列已满；当前语句已重置。"
            )
            self._cancel_session()

    def end(self, timestamp: float) -> None:
        if self._stop.is_set():
            return
        try:
            self.jobs.put_nowait(("end", b"", float(timestamp)))
        except queue.Full:
            self._cancel_session()

    def stop(self) -> None:
        if self._stop.is_set():
            return
        try:
            self.jobs.put(("stop", b"", time.time()), timeout=0.5)
        except queue.Full:
            self._stop.set()
            self._cancel_session()
        self._thread.join(timeout=3.0)
        if self._thread.is_alive():
            self._stop.set()
            self._cancel_session()

    def _ensure_client(self) -> tuple[Any, Any]:
        if self._client is not None:
            _, _, _, cloud_speech, _ = _load_google_modules()
            return self._client, cloud_speech
        _, _, speech_v2, cloud_speech, _ = _load_google_modules()
        credentials, project_id = resolve_google_credentials(self.config)
        location = str(self.config.location or "us").strip().lower()
        endpoint = f"{location}-speech.googleapis.com"
        target = endpoint + ":443"

        proxy = _normalize_proxy_url(self.config.proxy_url)
        if proxy and not proxy.lower().startswith("http://"):
            raise RuntimeError(
                "Google Speech-to-Text 的 gRPC 代理必须使用支持 HTTP CONNECT 的 "
                "HTTP/混合端口，例如 http://127.0.0.1:20800；不能直接使用 "
                "socks5:// 或 https:// 代理地址。"
            )

        # Build the authorized channel explicitly instead of relying on process-wide
        # proxy environment variables. gRPC channels are created lazily on some
        # versions, so restoring grpc_proxy immediately after SpeechClient() can
        # accidentally make the later RPC connect directly.
        import grpc
        import requests
        import google.auth.transport.grpc as google_auth_grpc
        import google.auth.transport.requests as google_auth_requests

        auth_session = requests.Session()
        auth_session.trust_env = False
        if proxy:
            auth_session.proxies.update({"http": proxy, "https": proxy})
        auth_request = google_auth_requests.Request(session=auth_session)
        channel_options: tuple[tuple[str, Any], ...] = ()
        if proxy:
            channel_options = (
                ("grpc.http_proxy", proxy),
                ("grpc.enable_http_proxy", 1),
            )
        channel = google_auth_grpc.secure_authorized_channel(
            credentials,
            auth_request,
            target,
            ssl_credentials=grpc.ssl_channel_credentials(),
            options=channel_options,
        )
        transport_class = speech_v2.SpeechClient.get_transport_class("grpc")
        transport = transport_class(channel=channel)
        self._client = speech_v2.SpeechClient(transport=transport)
        self._project_id = project_id
        logging.info(
            "Google Speech gRPC client endpoint=%s proxy=%s",
            target,
            proxy or "direct",
        )
        return self._client, cloud_speech

    def _open_session(self, timestamp: float) -> None:
        self._finish_session(wait=False)
        self._session_started_at = float(timestamp or time.time())
        self._session_done.clear()
        audio_queue: queue.Queue[Any] = queue.Queue(maxsize=2048)
        self._session_audio = audio_queue
        self._session_thread = threading.Thread(
            target=self._recognize_session,
            args=(audio_queue, self._session_started_at),
            name=f"GoogleSpeechGrpc-{self.source}",
            daemon=True,
        )
        self._session_thread.start()

    def _put_session_audio(self, pcm: bytes) -> None:
        audio_queue = self._session_audio
        if audio_queue is None:
            return
        # Cloud STT V2 documentation currently has 15 KB and 25 KB limits on
        # different reference pages. 6400 bytes (about 200 ms of 16 kHz mono
        # LINEAR16) stays safely below both.
        chunk_size = 6400
        for offset in range(0, len(pcm), chunk_size):
            chunk = pcm[offset : offset + chunk_size]
            if not chunk:
                continue
            try:
                audio_queue.put(chunk, timeout=0.5)
            except queue.Full:
                raise RuntimeError("Google 日语流式请求发送速度跟不上音频输入。")

    def _recognize_session(
        self, audio_queue: queue.Queue[Any], session_started_at: float
    ) -> None:
        latest_partial_text = ""
        latest_partial_timestamp = session_started_at
        finalized_keys: set[tuple[int, str]] = set()
        try:
            client, cloud_speech = self._ensure_client()
            location = str(self.config.location or "us").strip().lower()
            recognizer = (
                f"projects/{self._project_id}/locations/{location}/recognizers/_"
            )
            decoding = cloud_speech.ExplicitDecodingConfig(
                encoding=cloud_speech.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=int(self.config.sample_rate_hertz),
                audio_channel_count=1,
            )
            recognition_config = cloud_speech.RecognitionConfig(
                explicit_decoding_config=decoding,
                language_codes=[str(self.config.language_code or "ja-JP")],
                model=str(self.config.model or "chirp_3"),
                features=cloud_speech.RecognitionFeatures(
                    enable_automatic_punctuation=True,
                ),
            )
            streaming_config = cloud_speech.StreamingRecognitionConfig(
                config=recognition_config,
                streaming_features=cloud_speech.StreamingRecognitionFeatures(
                    interim_results=True,
                    enable_voice_activity_events=False,
                ),
            )
            config_request = cloud_speech.StreamingRecognizeRequest(
                recognizer=recognizer,
                streaming_config=streaming_config,
            )

            def requests():
                yield config_request
                while True:
                    item = audio_queue.get()
                    try:
                        if item is self._END:
                            return
                        yield cloud_speech.StreamingRecognizeRequest(audio=item)
                    finally:
                        audio_queue.task_done()

            self.signals.status.emit(self.source, "正在连接 Google 日语实时识别")
            responses = client.streaming_recognize(requests=requests())
            with self._lock:
                self._active_call = responses
            self.signals.status.emit(self.source, "已连接，正在低延迟增量识别日语")

            final_cursor = 0.0
            for response in responses:
                if self._stop.is_set():
                    break
                interim_parts: list[str] = []
                interim_end = final_cursor
                for result in getattr(response, "results", ()):
                    alternatives = getattr(result, "alternatives", ())
                    if not alternatives:
                        continue
                    text = str(alternatives[0].transcript or "").strip()
                    if not text:
                        continue
                    result_end = _duration_seconds(
                        getattr(result, "result_end_offset", None)
                    )
                    if bool(getattr(result, "is_final", False)):
                        start_offset = final_cursor
                        if result_end <= start_offset:
                            result_end = start_offset + max(0.2, len(text) * 0.08)
                        key = (int(start_offset * 1000), text)
                        if key in finalized_keys:
                            continue
                        finalized_keys.add(key)
                        timestamp = session_started_at + start_offset
                        end_timestamp = session_started_at + result_end
                        self.signals.final.emit(self.source, timestamp, text)
                        self.signals.segment_timing.emit(
                            self.source, timestamp, end_timestamp
                        )
                        final_cursor = max(final_cursor, result_end)
                        latest_partial_text = ""
                        latest_partial_timestamp = session_started_at + final_cursor
                    else:
                        interim_parts.append(text)
                        interim_end = max(interim_end, result_end)
                if interim_parts:
                    text = _join_result_parts(
                        interim_parts, str(self.config.language_code or "ja-JP")
                    )
                    timestamp = session_started_at + final_cursor
                    if text and (
                        text != latest_partial_text
                        or abs(timestamp - latest_partial_timestamp) > 0.001
                    ):
                        latest_partial_text = text
                        latest_partial_timestamp = timestamp
                        self.signals.partial.emit(self.source, timestamp, text)
                        if interim_end > final_cursor:
                            self.signals.segment_timing.emit(
                                self.source,
                                timestamp,
                                session_started_at + interim_end,
                            )
            if latest_partial_text and not self._stop.is_set():
                self.signals.final.emit(
                    self.source, latest_partial_timestamp, latest_partial_text
                )
        except Exception as exc:
            if not self._stop.is_set():
                logging.exception("Google Speech-to-Text streaming failed")
                self.signals.failed.emit(
                    self.source, _friendly_streaming_error(exc, self.config)
                )
        finally:
            with self._lock:
                self._active_call = None
            self._session_done.set()

    def _finish_session(self, *, wait: bool = True) -> None:
        audio_queue = self._session_audio
        thread = self._session_thread
        if audio_queue is not None:
            try:
                audio_queue.put(self._END, timeout=0.5)
            except queue.Full:
                self._cancel_active_call()
        if wait and thread is not None:
            self.signals.status.emit(self.source, "等待 Google 日语分句定稿")
            self._session_done.wait(timeout=max(2.0, self.config.final_timeout))
            if thread.is_alive():
                self._cancel_active_call()
                thread.join(timeout=1.0)
        self._session_audio = None
        self._session_thread = None
        self._session_started_at = 0.0

    def _cancel_active_call(self) -> None:
        with self._lock:
            call = self._active_call
        if call is not None and hasattr(call, "cancel"):
            try:
                call.cancel()
            except Exception:
                pass

    def _cancel_session(self) -> None:
        self._cancel_active_call()
        audio_queue = self._session_audio
        if audio_queue is not None:
            try:
                audio_queue.put_nowait(self._END)
            except Exception:
                pass
        thread = self._session_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.8)
        self._session_audio = None
        self._session_thread = None
        self._session_started_at = 0.0

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                kind, data, timestamp = self.jobs.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                if kind == "stop":
                    self._finish_session(wait=True)
                    self._stop.set()
                    break
                if kind == "end":
                    self._finish_session(wait=True)
                    self.signals.status.emit(self.source, "当前日语分句已完成")
                    continue
                if kind != "audio" or not data:
                    continue
                # A gRPC stream can terminate asynchronously (for example because
                # of a transient network error). Do not keep feeding an orphaned
                # queue; clean it up and open a fresh stream for subsequent audio.
                if self._session_audio is not None and self._session_done.is_set():
                    self._finish_session(wait=False)
                if self._session_audio is None:
                    self._open_session(timestamp)
                self._put_session_audio(data)
            except Exception as exc:
                if not self._stop.is_set():
                    logging.exception("Google Speech streaming worker failed")
                    self.signals.failed.emit(self.source, str(exc))
                self._cancel_session()
            finally:
                self.jobs.task_done()
        self._cancel_session()
