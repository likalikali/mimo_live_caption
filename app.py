from __future__ import annotations

import base64
from collections import deque
import html
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import queue
import re
import sys
import threading
import time
import wave
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import keyring
import numpy as np
import pyaudiowpatch as pyaudio
import requests

try:
    import webrtcvad
except ImportError:  # A pure-energy fallback remains available.
    webrtcvad = None
from PySide6.QtCore import QObject, QPoint, QThread, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QKeySequence, QPalette, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizeGrip,
    QSlider,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "MiMo Live Caption"
APP_VERSION = "0.6.0"
KEYRING_SERVICE = "MiMoLiveCaption"
MIMO_KEYRING_ACCOUNT = "mimo_api_key"
SILICONFLOW_KEYRING_ACCOUNT = "siliconflow_api_key"
MIMO_API_URL = "https://api.xiaomimimo.com/v1/chat/completions"
MIMO_MODELS_URL = "https://api.xiaomimimo.com/v1/models"
DEFAULT_SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
PROVIDER_NAMES = {
    "mimo": "MiMo",
    "siliconflow": "SiliconFlow",
}
SILICONFLOW_MODELS = (
    "FunAudioLLM/SenseVoiceSmall",
    "TeleAI/TeleSpeechASR",
)
TARGET_RATE = 16_000
SAMPLE_WIDTH = 2


# ------------------------------- paths / logging -------------------------------

def app_data_dir() -> Path:
    root = os.getenv("APPDATA")
    if root:
        return Path(root) / "MiMoLiveCaption"
    return Path.home() / ".mimo_live_caption"


def config_path() -> Path:
    return app_data_dir() / "config.json"


def log_path() -> Path:
    return app_data_dir() / "app.log"


def setup_logging() -> None:
    app_data_dir().mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(threadName)s %(message)s"
    )
    file_handler = RotatingFileHandler(
        log_path(), maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if getattr(sys, "stdout", None):
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        root.addHandler(console)


# ---------------------------------- config ----------------------------------

@dataclass
class AppConfig:
    language: str = "auto"
    # Legacy fixed segmentation settings are retained for compatibility/migration.
    chunk_seconds: float = 4.0
    overlap_seconds: float = 0.35
    silence_threshold: int = 70
    segmentation_mode: str = "vad"
    vad_mode: int = 2
    speech_start_ms: int = 80
    speech_end_ms: int = 600
    pre_roll_ms: int = 240
    post_roll_ms: int = 180
    min_speech_ms: int = 120
    max_segment_seconds: float = 10.0
    stream_response: bool = True
    panel_alpha: int = 190
    font_size: int = 16
    mic_device_key: str = ""
    system_device_key: str = ""
    capture_mic: bool = True
    capture_system: bool = True
    mic_compat_mode: bool = True
    prevent_windows_ducking: bool = True
    ducking_prompt_answered: bool = False
    api_provider: str = "mimo"
    mimo_model: str = "mimo-v2.5-asr"
    siliconflow_model: str = "FunAudioLLM/SenseVoiceSmall"
    siliconflow_base_url: str = DEFAULT_SILICONFLOW_BASE_URL

    @classmethod
    def load(cls) -> "AppConfig":
        try:
            data = json.loads(config_path().read_text(encoding="utf-8"))
            valid = {k: data[k] for k in cls.__annotations__ if k in data}
            return cls(**valid)
        except FileNotFoundError:
            return cls()
        except Exception:
            logging.exception("Failed to load config")
            return cls()

    def save(self) -> None:
        app_data_dir().mkdir(parents=True, exist_ok=True)
        config_path().write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _load_keyring_value(account: str) -> str:
    try:
        return (keyring.get_password(KEYRING_SERVICE, account) or "").strip()
    except Exception:
        logging.exception("Failed to read API key from keyring account=%s", account)
        return ""


def load_api_key(provider: str) -> str:
    if provider == "siliconflow":
        env_key = os.getenv("SILICONFLOW_API_KEY", "").strip()
        return env_key or _load_keyring_value(SILICONFLOW_KEYRING_ACCOUNT)
    env_key = os.getenv("MIMO_API_KEY", "").strip()
    return env_key or _load_keyring_value(MIMO_KEYRING_ACCOUNT)


def save_api_key(provider: str, api_key: str) -> None:
    api_key = api_key.strip()
    if not api_key:
        return
    account = (
        SILICONFLOW_KEYRING_ACCOUNT
        if provider == "siliconflow"
        else MIMO_KEYRING_ACCOUNT
    )
    keyring.set_password(KEYRING_SERVICE, account, api_key)


def provider_display_name(provider: str) -> str:
    return PROVIDER_NAMES.get(provider, provider or "未知平台")


# --------------------------- Windows communication ducking ---------------------------

def get_ducking_preference() -> Optional[int]:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Multimedia\Audio",
            0,
            winreg.KEY_READ,
        ) as key:
            value, _ = winreg.QueryValueEx(key, "UserDuckingPreference")
            return int(value)
    except FileNotFoundError:
        return None
    except Exception:
        logging.exception("Could not read UserDuckingPreference")
        return None


def set_ducking_do_nothing() -> None:
    if sys.platform != "win32":
        return
    import winreg

    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Multimedia\Audio",
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        winreg.SetValueEx(key, "UserDuckingPreference", 0, winreg.REG_DWORD, 3)
    logging.info("Set Windows UserDuckingPreference=3 (Do nothing)")


# -------------------------------- devices --------------------------------

@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    host_api: str
    channels: int
    rate: int
    is_loopback: bool

    @property
    def key(self) -> str:
        return f"{self.host_api}|{self.name}|{'loopback' if self.is_loopback else 'input'}"

    @property
    def display_name(self) -> str:
        suffix = "，推荐兼容模式" if self.host_api.lower().endswith("mme") else ""
        return f"[{self.host_api}] {self.name} ({self.channels}ch/{self.rate}Hz{suffix})"


@dataclass
class DeviceInventory:
    microphones: list[AudioDevice]
    loopbacks: list[AudioDevice]
    default_input_index: Optional[int]
    default_output_name: str


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _base_name(name: str) -> str:
    value = re.sub(r"\s*\[Loopback\]\s*$", "", name, flags=re.I)
    value = value.lower().replace("（", "(").replace("）", ")")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def scan_audio_devices() -> DeviceInventory:
    manager = pyaudio.PyAudio()
    try:
        default_input_index: Optional[int] = None
        default_output_name = ""
        try:
            default_input_index = int(manager.get_default_input_device_info()["index"])
        except Exception:
            pass

        try:
            wasapi = manager.get_host_api_info_by_type(pyaudio.paWASAPI)
            output_index = _safe_int(wasapi.get("defaultOutputDevice"), -1)
            if output_index >= 0:
                output_info = manager.get_device_info_by_index(output_index)
                if _safe_int(output_info.get("maxOutputChannels")) > 0:
                    default_output_name = str(output_info.get("name", ""))
        except Exception:
            logging.exception("Could not resolve default WASAPI output")

        microphones: list[AudioDevice] = []
        loopbacks: list[AudioDevice] = []
        for index in range(manager.get_device_count()):
            try:
                info = manager.get_device_info_by_index(index)
                channels = _safe_int(info.get("maxInputChannels"))
                if channels <= 0:
                    continue
                host_index = _safe_int(info.get("hostApi"), -1)
                host_name = (
                    str(manager.get_host_api_info_by_index(host_index).get("name", "Unknown"))
                    if host_index >= 0
                    else "Unknown"
                )
                name = str(info.get("name", f"Device {index}"))
                is_loopback = bool(info.get("isLoopbackDevice", False)) or (
                    "[loopback]" in name.lower()
                )
                device = AudioDevice(
                    index=index,
                    name=name,
                    host_api=host_name,
                    channels=channels,
                    rate=max(8_000, _safe_int(info.get("defaultSampleRate"), 48_000)),
                    is_loopback=is_loopback,
                )
                if is_loopback:
                    loopbacks.append(device)
                else:
                    microphones.append(device)
            except Exception:
                logging.exception("Failed reading audio device index=%s", index)

        logging.info(
            "Audio scan: microphones=%d loopbacks=%d default_input=%s default_output=%r",
            len(microphones),
            len(loopbacks),
            default_input_index,
            default_output_name,
        )
        return DeviceInventory(
            microphones=microphones,
            loopbacks=loopbacks,
            default_input_index=default_input_index,
            default_output_name=default_output_name,
        )
    finally:
        manager.terminate()


def _host_rank(host_api: str) -> int:
    lower = host_api.lower()
    if "mme" in lower:
        return 0
    if "directsound" in lower:
        return 1
    if "wasapi" in lower:
        return 3
    return 2


def resolve_microphone(
    inventory: DeviceInventory, configured_key: str, compat_mode: bool
) -> Optional[AudioDevice]:
    devices = inventory.microphones
    if not devices:
        return None

    selected = next((d for d in devices if d.key == configured_key), None)
    if selected is None and inventory.default_input_index is not None:
        selected = next(
            (d for d in devices if d.index == inventory.default_input_index), None
        )
    if selected is None:
        selected = sorted(devices, key=lambda d: (_host_rank(d.host_api), d.index))[0]

    if compat_mode and "wasapi" in selected.host_api.lower():
        selected_base = _base_name(selected.name)
        same_device = [
            d
            for d in devices
            if _base_name(d.name) == selected_base and "wasapi" not in d.host_api.lower()
        ]
        if same_device:
            replacement = sorted(same_device, key=lambda d: (_host_rank(d.host_api), d.index))[0]
            logging.info(
                "Microphone compatibility remap: %r [%s] -> %r [%s]",
                selected.name,
                selected.host_api,
                replacement.name,
                replacement.host_api,
            )
            selected = replacement
    return selected


def resolve_loopback(
    inventory: DeviceInventory, configured_key: str
) -> Optional[AudioDevice]:
    devices = inventory.loopbacks
    if not devices:
        return None
    configured = next((d for d in devices if d.key == configured_key), None)
    if configured:
        return configured

    output_base = _base_name(inventory.default_output_name)
    if output_base:
        exact = [d for d in devices if _base_name(d.name) == output_base]
        if exact:
            return exact[0]
        fuzzy = [
            d
            for d in devices
            if output_base in _base_name(d.name) or _base_name(d.name) in output_base
        ]
        if fuzzy:
            return fuzzy[0]
    return devices[0]


# -------------------------------- audio helpers --------------------------------

def pcm_rms(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples * samples)))


def level_percent(rms: float) -> int:
    if rms <= 0:
        return 0
    db = 20.0 * math.log10(max(rms, 1.0) / 32768.0)
    return max(0, min(100, int((db + 60.0) / 60.0 * 100.0)))


def convert_to_mono_16k(raw: bytes, channels: int, source_rate: int) -> bytes:
    samples = np.frombuffer(raw, dtype=np.int16)
    if samples.size == 0:
        return b""
    channels = max(1, int(channels))
    usable = samples.size - (samples.size % channels)
    if usable <= 0:
        return b""
    frames = samples[:usable].reshape(-1, channels).astype(np.float32)
    mono = frames.mean(axis=1)
    if source_rate != TARGET_RATE and mono.size > 1:
        out_len = max(1, int(round(mono.size * TARGET_RATE / source_rate)))
        old_x = np.linspace(0.0, 1.0, num=mono.size, endpoint=False)
        new_x = np.linspace(0.0, 1.0, num=out_len, endpoint=False)
        mono = np.interp(new_x, old_x, mono)
    return np.clip(mono, -32768, 32767).astype(np.int16).tobytes()


def make_wav(pcm: bytes, rate: int = TARGET_RATE) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(SAMPLE_WIDTH)
        wav_file.setframerate(rate)
        wav_file.writeframes(pcm)
    return buffer.getvalue()


class AdaptiveVadSegmenter:
    """Turn 16 kHz mono PCM into utterances using WebRTC VAD plus adaptive energy.

    The pre-roll prevents clipped first syllables.  A trailing-silence hangover ends
    the utterance quickly, while max_segment_seconds guarantees progress when a
    speaker talks continuously.
    """

    FRAME_MS = 20
    FRAME_BYTES = TARGET_RATE * SAMPLE_WIDTH * FRAME_MS // 1000

    def __init__(
        self,
        *,
        vad_mode: int,
        min_energy: int,
        speech_start_ms: int,
        speech_end_ms: int,
        pre_roll_ms: int,
        post_roll_ms: int,
        min_speech_ms: int,
        max_segment_seconds: float,
    ) -> None:
        self.vad_mode = max(0, min(3, int(vad_mode)))
        self.min_energy = max(1, int(min_energy))
        self.start_frames = max(1, math.ceil(speech_start_ms / self.FRAME_MS))
        self.end_frames = max(1, math.ceil(speech_end_ms / self.FRAME_MS))
        self.pre_roll_frames = max(0, math.ceil(pre_roll_ms / self.FRAME_MS))
        self.post_roll_frames = max(0, math.ceil(post_roll_ms / self.FRAME_MS))
        self.min_voiced_frames = max(1, math.ceil(min_speech_ms / self.FRAME_MS))
        self.max_frames = max(
            self.min_voiced_frames,
            math.ceil(max_segment_seconds * 1000 / self.FRAME_MS),
        )
        self.split_overlap_frames = min(
            self.pre_roll_frames,
            max(1, math.ceil(200 / self.FRAME_MS)),
        )
        self.pending = bytearray()
        self.pre_roll: deque[tuple[bytes, bool]] = deque(maxlen=self.pre_roll_frames)
        self.start_window: deque[bool] = deque(maxlen=self.start_frames)
        self.segment: list[tuple[bytes, bool]] = []
        self.active = False
        self.silence_run = 0
        self.voiced_frames = 0
        self.started_at = 0.0
        self.noise_floor = float(self.min_energy)
        self.dynamic_threshold = float(self.min_energy)
        self._vad = webrtcvad.Vad(self.vad_mode) if webrtcvad is not None else None

    @property
    def state(self) -> str:
        return "speech" if self.active else "idle"

    def _is_speech_frame(self, frame: bytes, rms: float) -> bool:
        # Learn the background only outside an active utterance. A slow rise lets
        # the threshold follow fans/room noise without chasing the speaker.
        if not self.active:
            alpha = 0.015 if rms < self.noise_floor * 2.5 else 0.002
            self.noise_floor = (1.0 - alpha) * self.noise_floor + alpha * rms
        self.dynamic_threshold = max(
            float(self.min_energy),
            min(4000.0, self.noise_floor * 2.15 + 18.0),
        )
        energy_speech = rms >= self.dynamic_threshold
        if self._vad is None:
            return energy_speech
        try:
            vad_speech = bool(self._vad.is_speech(frame, TARGET_RATE))
        except Exception:
            logging.exception("WebRTC VAD classification failed; using energy fallback")
            return energy_speech
        # WebRTC rejects much stationary noise/music; adaptive energy rejects
        # digital silence and very faint false positives.
        return vad_speech and energy_speech

    def _reset_to_idle(self, tail: list[tuple[bytes, bool]] | None = None) -> None:
        self.active = False
        self.segment = []
        self.silence_run = 0
        self.voiced_frames = 0
        self.started_at = 0.0
        self.start_window.clear()
        self.pre_roll.clear()
        if tail and self.pre_roll_frames:
            for item in tail[-self.pre_roll_frames :]:
                self.pre_roll.append(item)

    def _emit_segment(self, reason: str, trim_end: bool) -> Optional[tuple[bytes, float, str]]:
        frames = self.segment
        if trim_end and self.silence_run > self.post_roll_frames:
            frames = frames[: -(self.silence_run - self.post_roll_frames)]
        if self.voiced_frames < self.min_voiced_frames or not frames:
            return None
        return b"".join(frame for frame, _ in frames), self.started_at, reason

    def feed(self, pcm: bytes, wall_time: float) -> list[tuple[bytes, float, str]]:
        self.pending.extend(pcm)
        output: list[tuple[bytes, float, str]] = []
        while len(self.pending) >= self.FRAME_BYTES:
            frame = bytes(self.pending[: self.FRAME_BYTES])
            del self.pending[: self.FRAME_BYTES]
            rms = pcm_rms(frame)
            voiced = self._is_speech_frame(frame, rms)

            if not self.active:
                if self.pre_roll_frames:
                    self.pre_roll.append((frame, voiced))
                self.start_window.append(voiced)
                # Require consecutive voiced frames. Pre-roll absorbs this decision
                # delay, so the first syllable is still included.
                if len(self.start_window) == self.start_frames and all(self.start_window):
                    self.active = True
                    self.segment = list(self.pre_roll) if self.pre_roll else [(frame, voiced)]
                    self.voiced_frames = sum(1 for _, flag in self.segment if flag)
                    self.silence_run = 0
                    self.started_at = wall_time - len(self.segment) * self.FRAME_MS / 1000.0
                    self.pre_roll.clear()
                    self.start_window.clear()
                continue

            self.segment.append((frame, voiced))
            if voiced:
                self.voiced_frames += 1
                self.silence_run = 0
            else:
                self.silence_run += 1

            if len(self.segment) >= self.max_frames:
                emitted = self._emit_segment("max_duration", trim_end=False)
                if emitted:
                    output.append(emitted)
                overlap = self.segment[-self.split_overlap_frames :]
                self.segment = overlap
                self.voiced_frames = sum(1 for _, flag in overlap if flag)
                self.silence_run = 0
                for _, flag in reversed(overlap):
                    if flag:
                        break
                    self.silence_run += 1
                self.started_at = wall_time - len(overlap) * self.FRAME_MS / 1000.0
                continue

            if self.silence_run >= self.end_frames:
                emitted = self._emit_segment("silence", trim_end=True)
                tail = self.segment[-self.pre_roll_frames :] if self.pre_roll_frames else []
                if emitted:
                    output.append(emitted)
                self._reset_to_idle(tail)

        return output

    def flush(self) -> list[tuple[bytes, float, str]]:
        output: list[tuple[bytes, float, str]] = []
        if self.active:
            emitted = self._emit_segment("stop", trim_end=True)
            if emitted:
                output.append(emitted)
        self.pending.clear()
        self._reset_to_idle()
        return output


# ----------------------------- callback-based audio engine -----------------------------

class AudioEngine(QThread):
    chunk_ready = Signal(str, bytes, float)
    level_changed = Signal(str, int, int)
    device_opened = Signal(str, str)
    vad_state_changed = Signal(str, str)
    failed = Signal(str, str)
    engine_stopped = Signal()

    def __init__(
        self,
        mic_device: Optional[AudioDevice],
        system_device: Optional[AudioDevice],
        config: AppConfig,
    ) -> None:
        super().__init__()
        self.setObjectName("AudioEngine")
        self.devices = {"mic": mic_device, "system": system_device}
        self.config = config
        self._stop_event = threading.Event()
        self._raw_queues: dict[str, queue.Queue[bytes]] = {
            "mic": queue.Queue(maxsize=128),
            "system": queue.Queue(maxsize=128),
        }
        self._callback_refs: dict[str, Callable[..., Any]] = {}

    def request_stop(self) -> None:
        self._stop_event.set()

    def _make_callback(self, source: str) -> Callable[..., Any]:
        def callback(in_data, frame_count, time_info, status_flags):
            del frame_count, time_info
            if status_flags:
                logging.warning("PortAudio status source=%s flags=%s", source, status_flags)
            if self._stop_event.is_set():
                return (None, pyaudio.paComplete)
            q = self._raw_queues[source]
            try:
                q.put_nowait(in_data)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(in_data)
                except queue.Full:
                    pass
            return (None, pyaudio.paContinue)

        self._callback_refs[source] = callback
        return callback

    @staticmethod
    def _channel_candidates(source: str, max_channels: int) -> list[int]:
        if source == "mic":
            candidates = [1, min(2, max_channels), max_channels]
        else:
            candidates = [min(2, max_channels), max_channels, 1]
        result: list[int] = []
        for value in candidates:
            if 1 <= value <= max_channels and value not in result:
                result.append(value)
        return result

    @staticmethod
    def _rate_candidates(default_rate: int) -> list[int]:
        result: list[int] = []
        for rate in (default_rate, 48_000, 44_100):
            if rate > 0 and rate not in result:
                result.append(rate)
        return result

    def _open_input_only_stream(
        self, manager: pyaudio.PyAudio, source: str, device: AudioDevice
    ) -> tuple[Any, int, int]:
        errors: list[str] = []
        for channels in self._channel_candidates(source, device.channels):
            for rate in self._rate_candidates(device.rate):
                try:
                    stream = manager.open(
                        format=pyaudio.paInt16,
                        channels=channels,
                        rate=rate,
                        input=True,
                        output=False,
                        input_device_index=device.index,
                        frames_per_buffer=1024,
                        stream_callback=self._make_callback(source),
                        start=False,
                    )
                    logging.info(
                        "Opened %s input-only shared capture index=%d name=%r host=%r "
                        "channels=%d rate=%d loopback=%s output=False",
                        source,
                        device.index,
                        device.name,
                        device.host_api,
                        channels,
                        rate,
                        device.is_loopback,
                    )
                    return stream, channels, rate
                except Exception as exc:
                    errors.append(f"{channels}ch/{rate}Hz: {exc}")
        raise RuntimeError("；".join(errors[-4:]))

    def _make_segmenter(self) -> AdaptiveVadSegmenter:
        return AdaptiveVadSegmenter(
            vad_mode=self.config.vad_mode,
            min_energy=self.config.silence_threshold,
            speech_start_ms=self.config.speech_start_ms,
            speech_end_ms=self.config.speech_end_ms,
            pre_roll_ms=self.config.pre_roll_ms,
            post_roll_ms=self.config.post_roll_ms,
            min_speech_ms=self.config.min_speech_ms,
            max_segment_seconds=self.config.max_segment_seconds,
        )

    def _emit_pcm_segment(
        self, source: str, pcm: bytes, timestamp: float, reason: str
    ) -> None:
        duration = len(pcm) / SAMPLE_WIDTH / TARGET_RATE
        segment_rms = pcm_rms(pcm)
        logging.info(
            "Adaptive segment source=%s seconds=%.2f rms=%.1f reason=%s",
            source,
            duration,
            segment_rms,
            reason,
        )
        self.chunk_ready.emit(source, make_wav(pcm), timestamp)
        self.vad_state_changed.emit(source, "sent")

    def run(self) -> None:
        manager: Optional[pyaudio.PyAudio] = None
        streams: dict[str, Any] = {}
        stream_formats: dict[str, tuple[int, int]] = {}
        fixed_buffers = {"mic": bytearray(), "system": bytearray()}
        segmenters: dict[str, AdaptiveVadSegmenter] = {}
        previous_states = {"mic": "idle", "system": "idle"}
        latest_rms = {"mic": 0.0, "system": 0.0}
        last_level_emit = 0.0
        chunk_bytes = int(TARGET_RATE * self.config.chunk_seconds) * SAMPLE_WIDTH
        overlap_bytes = int(TARGET_RATE * self.config.overlap_seconds) * SAMPLE_WIDTH
        overlap_bytes = min(overlap_bytes, max(0, chunk_bytes - SAMPLE_WIDTH))

        try:
            manager = pyaudio.PyAudio()
            logging.info(
                "PortAudio initialized segmentation=%s webrtcvad=%s end_ms=%d max_seconds=%.1f",
                self.config.segmentation_mode,
                webrtcvad is not None,
                self.config.speech_end_ms,
                self.config.max_segment_seconds,
            )

            for source in ("mic", "system"):
                device = self.devices[source]
                if device is None:
                    continue
                try:
                    stream, channels, rate = self._open_input_only_stream(manager, source, device)
                    streams[source] = stream
                    stream_formats[source] = (channels, rate)
                    if self.config.segmentation_mode == "vad":
                        segmenters[source] = self._make_segmenter()
                    self.device_opened.emit(source, device.display_name)
                    self.vad_state_changed.emit(source, "idle")
                except Exception as exc:
                    logging.exception("Could not open %s", source)
                    self.failed.emit(source, str(exc))

            if not streams:
                raise RuntimeError("没有任何音频设备可以打开。")

            for stream in streams.values():
                stream.start_stream()

            while not self._stop_event.is_set():
                processed_any = False
                for source in streams:
                    q = self._raw_queues[source]
                    channels, rate = stream_formats[source]
                    drained = 0
                    while drained < 32:
                        try:
                            raw = q.get_nowait()
                        except queue.Empty:
                            break
                        drained += 1
                        processed_any = True
                        pcm = convert_to_mono_16k(raw, channels, rate)
                        if not pcm:
                            continue
                        value = pcm_rms(pcm)
                        latest_rms[source] = max(value, latest_rms[source] * 0.72)

                        if self.config.segmentation_mode == "vad":
                            segmenter = segmenters[source]
                            for data, timestamp, reason in segmenter.feed(pcm, time.time()):
                                self._emit_pcm_segment(source, data, timestamp, reason)
                            state = segmenter.state
                            if state != previous_states[source]:
                                previous_states[source] = state
                                self.vad_state_changed.emit(source, state)
                        else:
                            fixed_buffers[source].extend(pcm)
                            while len(fixed_buffers[source]) >= chunk_bytes:
                                chunk = bytes(fixed_buffers[source][:chunk_bytes])
                                advance = chunk_bytes - overlap_bytes
                                del fixed_buffers[source][:advance]
                                chunk_rms = pcm_rms(chunk)
                                accepted = chunk_rms >= self.config.silence_threshold
                                logging.info(
                                    "Fixed segment source=%s seconds=%.2f rms=%.1f accepted=%s",
                                    source,
                                    len(chunk) / SAMPLE_WIDTH / TARGET_RATE,
                                    chunk_rms,
                                    accepted,
                                )
                                if accepted:
                                    self.chunk_ready.emit(source, make_wav(chunk), time.time())

                now = time.monotonic()
                if now - last_level_emit >= 0.1:
                    last_level_emit = now
                    for source in ("mic", "system"):
                        latest_rms[source] *= 0.88
                        self.level_changed.emit(
                            source,
                            level_percent(latest_rms[source]),
                            int(latest_rms[source]),
                        )
                if not processed_any:
                    time.sleep(0.01)

            for source in streams:
                if self.config.segmentation_mode == "vad":
                    for data, timestamp, reason in segmenters[source].flush():
                        self._emit_pcm_segment(source, data, timestamp, reason)
                else:
                    data = bytes(fixed_buffers[source])
                    if (
                        len(data) >= TARGET_RATE * SAMPLE_WIDTH
                        and pcm_rms(data) >= self.config.silence_threshold
                    ):
                        self.chunk_ready.emit(source, make_wav(data), time.time())

        except Exception as exc:
            logging.exception("Audio engine failed")
            self.failed.emit("engine", str(exc))
        finally:
            for source, stream in streams.items():
                try:
                    if stream.is_active():
                        stream.stop_stream()
                except Exception:
                    logging.exception("Failed stopping %s stream", source)
                try:
                    stream.close()
                except Exception:
                    logging.exception("Failed closing %s stream", source)
            if manager is not None:
                try:
                    manager.terminate()
                except Exception:
                    logging.exception("Failed terminating PortAudio")
            logging.info("Audio engine stopped cleanly")
            self.engine_stopped.emit()


# -------------------------------- MiMo API --------------------------------

def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            for key in ("text", "transcript", "output_text", "content"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
                    break
    return "".join(parts).strip()


def extract_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message", {}) if isinstance(first, dict) else {}
        if isinstance(message, dict):
            text = content_to_text(message.get("content"))
            if text:
                return text
        delta = first.get("delta", {}) if isinstance(first, dict) else {}
        if isinstance(delta, dict):
            text = content_to_text(delta.get("content"))
            if text:
                return text
    for key in ("text", "transcript", "output_text", "result"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = extract_text(value)
            if nested:
                return nested
    return ""


def extract_stream_piece(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0] if isinstance(choices[0], dict) else {}
    delta = first.get("delta", {}) if isinstance(first, dict) else {}
    if isinstance(delta, dict):
        text = content_to_text(delta.get("content"))
        if text:
            return text
    message = first.get("message", {}) if isinstance(first, dict) else {}
    if isinstance(message, dict):
        return content_to_text(message.get("content"))
    return ""


def append_stream_piece(accumulated: str, piece: str) -> str:
    """Accept both OpenAI-style deltas and providers that send cumulative text."""
    piece = piece or ""
    if not piece:
        return accumulated
    if piece.startswith(accumulated):
        return piece
    if accumulated.endswith(piece):
        return accumulated
    return accumulated + piece


def remove_overlap(previous: str, current: str, max_chars: int = 50) -> str:
    previous = previous.strip()
    current = current.strip()
    if not previous or not current:
        return current
    limit = min(max_chars, len(previous), len(current))
    for size in range(limit, 1, -1):
        if previous[-size:] == current[:size]:
            return current[size:].lstrip("，。！？,.!?;；:： ")
    return current


class MiMoClient:
    supports_streaming = True

    def __init__(self, api_key: str, language: str, model: str) -> None:
        self.api_key = api_key
        self.language = language
        self.model = model or "mimo-v2.5-asr"
        self.session = requests.Session()

    @property
    def headers(self) -> dict[str, str]:
        return {"api-key": self.api_key, "Content-Type": "application/json"}

    def _payload(self, wav_bytes: bytes, stream: bool = False) -> dict[str, Any]:
        encoded = base64.b64encode(wav_bytes).decode("ascii")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": f"data:audio/wav;base64,{encoded}"
                            },
                        }
                    ],
                }
            ],
            "asr_options": {"language": self.language},
        }
        if stream:
            payload["stream"] = True
        return payload

    def validate(self) -> None:
        response = self.session.get(MIMO_MODELS_URL, headers=self.headers, timeout=(8, 20))
        if response.status_code >= 400:
            raise RuntimeError(f"MiMo API {response.status_code}: {response.text[:300]}")

    def transcribe(self, wav_bytes: bytes) -> str:
        response = self.session.post(
            MIMO_API_URL,
            headers=self.headers,
            json=self._payload(wav_bytes),
            timeout=(10, 60),
        )
        if response.status_code >= 400:
            raise RuntimeError(f"MiMo API {response.status_code}: {response.text[:500]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(f"MiMo 返回了非 JSON 内容：{response.text[:500]}") from exc
        text = extract_text(data)
        if not text:
            raise RuntimeError(
                "MiMo 接口成功但未找到文本：" + json.dumps(data, ensure_ascii=False)[:600]
            )
        return text

    def transcribe_stream(self, wav_bytes: bytes):
        response = self.session.post(
            MIMO_API_URL,
            headers=self.headers,
            json=self._payload(wav_bytes, stream=True),
            stream=True,
            timeout=(10, 90),
        )
        if response.status_code >= 400:
            raise RuntimeError(f"MiMo API {response.status_code}: {response.text[:500]}")
        response.encoding = "utf-8"
        accumulated = ""
        raw_payload_lines: list[str] = []
        for raw_line in response.iter_lines(decode_unicode=True):
            if raw_line is None:
                continue
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                break
            try:
                payload = json.loads(line)
            except ValueError:
                raw_payload_lines.append(line)
                continue
            piece = extract_stream_piece(payload)
            if piece:
                accumulated += piece
                yield piece

        if not accumulated and raw_payload_lines:
            joined = "".join(raw_payload_lines)
            try:
                payload = json.loads(joined)
            except ValueError as exc:
                raise RuntimeError(f"MiMo 流式响应无法解析：{joined[:500]}") from exc
            text = extract_text(payload)
            if text:
                accumulated = text
                yield text
        if not accumulated:
            raise RuntimeError("MiMo 流式响应结束，但没有返回转写文字。")


class SiliconFlowClient:
    supports_streaming = False

    def __init__(self, api_key: str, model: str, base_url: str) -> None:
        self.api_key = api_key
        self.model = model or SILICONFLOW_MODELS[0]
        self.base_url = (base_url or DEFAULT_SILICONFLOW_BASE_URL).rstrip("/")
        self.session = requests.Session()

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def validate(self) -> None:
        response = self.session.get(
            f"{self.base_url}/models",
            headers=self.headers,
            params={"sub_type": "speech-to-text"},
            timeout=(8, 20),
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"SiliconFlow API {response.status_code}: {response.text[:300]}"
            )
        try:
            payload = response.json()
        except ValueError:
            return
        models = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(models, list) and models:
            model_ids = {
                str(item.get("id", "")) for item in models if isinstance(item, dict)
            }
            if model_ids and self.model not in model_ids:
                raise RuntimeError(
                    f"API 已连接，但模型 {self.model} 不在当前 speech-to-text 模型列表中。"
                )

    def transcribe(self, wav_bytes: bytes) -> str:
        response = self.session.post(
            f"{self.base_url}/audio/transcriptions",
            headers=self.headers,
            files={"file": ("audio.wav", wav_bytes, "audio/wav")},
            data={"model": self.model},
            timeout=(10, 90),
        )
        trace_id = response.headers.get("x-siliconcloud-trace-id", "")
        trace_text = f"，trace-id={trace_id}" if trace_id else ""
        if response.status_code >= 400:
            raise RuntimeError(
                f"SiliconFlow API {response.status_code}{trace_text}: {response.text[:500]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"SiliconFlow 返回了非 JSON 内容{trace_text}：{response.text[:500]}"
            ) from exc
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError(
                "SiliconFlow 接口成功但未找到 text 字段"
                f"{trace_text}：{json.dumps(payload, ensure_ascii=False)[:600]}"
            )
        return text.strip()


def create_asr_client(config: AppConfig, api_key: str) -> Any:
    if config.api_provider == "siliconflow":
        return SiliconFlowClient(
            api_key=api_key,
            model=config.siliconflow_model,
            base_url=config.siliconflow_base_url,
        )
    return MiMoClient(
        api_key=api_key,
        language=config.language,
        model=config.mimo_model,
    )


class ApiSignals(QObject):
    partial = Signal(str, float, str)
    result = Signal(str, float, str)
    failed = Signal(str, str)
    queue_size = Signal(str, int)
    validation = Signal(bool, str)


class ApiWorker:
    def __init__(self, source: str, api_key: str, config: AppConfig, signals: ApiSignals):
        self.source = source
        self.provider = config.api_provider
        self.model = (
            config.siliconflow_model
            if config.api_provider == "siliconflow"
            else config.mimo_model
        )
        self.client = create_asr_client(config, api_key)
        self.use_stream = bool(
            config.stream_response and getattr(self.client, "supports_streaming", False)
        )
        self.signals = signals
        self.jobs: queue.Queue[Optional[tuple[bytes, float]]] = queue.Queue(maxsize=5)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name=f"ASR-{self.provider}-{source}",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()

    def submit(self, wav_bytes: bytes, timestamp: float) -> None:
        if self.stop_event.is_set():
            return
        try:
            self.jobs.put_nowait((wav_bytes, timestamp))
        except queue.Full:
            try:
                self.jobs.get_nowait()
            except queue.Empty:
                pass
            try:
                self.jobs.put_nowait((wav_bytes, timestamp))
            except queue.Full:
                pass
        self.signals.queue_size.emit(self.source, self.jobs.qsize())

    def stop(self) -> None:
        self.stop_event.set()
        try:
            self.jobs.put_nowait(None)
        except queue.Full:
            pass

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                job = self.jobs.get(timeout=0.25)
            except queue.Empty:
                continue
            if job is None:
                break
            wav_bytes, timestamp = job
            started = time.monotonic()
            try:
                seconds = max(0.0, (len(wav_bytes) - 44) / SAMPLE_WIDTH / TARGET_RATE)
                logging.info(
                    "Submitting audio provider=%s model=%s source=%s seconds=%.2f bytes=%d stream=%s",
                    self.provider,
                    self.model,
                    self.source,
                    seconds,
                    len(wav_bytes),
                    self.use_stream,
                )
                if self.use_stream:
                    accumulated = ""
                    for piece in self.client.transcribe_stream(wav_bytes):
                        if self.stop_event.is_set():
                            break
                        accumulated += piece
                        if accumulated:
                            self.signals.partial.emit(self.source, timestamp, accumulated)
                    text = accumulated
                    if not text and not self.stop_event.is_set():
                        raise RuntimeError("流式响应没有返回文字。")
                else:
                    text = self.client.transcribe(wav_bytes)
                if self.stop_event.is_set():
                    continue
                logging.info(
                    "Transcription succeeded source=%s elapsed=%.2fs chars=%d stream=%s",
                    self.source,
                    time.monotonic() - started,
                    len(text),
                    self.use_stream,
                )
                self.signals.result.emit(self.source, timestamp, text)
            except Exception as exc:
                logging.exception("Transcription failed source=%s", self.source)
                if not self.stop_event.is_set():
                    self.signals.failed.emit(self.source, str(exc))
            self.signals.queue_size.emit(self.source, self.jobs.qsize())


# -------------------------------- settings UI --------------------------------

class SettingsDialog(QDialog):
    def __init__(self, config: AppConfig, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(780)
        self.config = config
        self.inventory: Optional[DeviceInventory] = None

        self.provider = QComboBox()
        self.provider.addItem("MiMo", "mimo")
        self.provider.addItem("SiliconFlow", "siliconflow")
        self.provider.setCurrentIndex(max(0, self.provider.findData(config.api_provider)))
        self._model_values = {
            "mimo": config.mimo_model,
            "siliconflow": config.siliconflow_model,
        }
        self._current_provider = config.api_provider
        self.provider.currentIndexChanged.connect(self.on_provider_changed)

        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setInsertPolicy(QComboBox.NoInsert)

        self.mimo_api_key = QLineEdit(load_api_key("mimo"))
        self.mimo_api_key.setEchoMode(QLineEdit.Password)
        self.mimo_api_key.setPlaceholderText("也可使用环境变量 MIMO_API_KEY")
        self.siliconflow_api_key = QLineEdit(load_api_key("siliconflow"))
        self.siliconflow_api_key.setEchoMode(QLineEdit.Password)
        self.siliconflow_api_key.setPlaceholderText(
            "也可使用环境变量 SILICONFLOW_API_KEY"
        )
        self.siliconflow_base_url = QLineEdit(config.siliconflow_base_url)
        self.siliconflow_base_url.setPlaceholderText(DEFAULT_SILICONFLOW_BASE_URL)

        self.language = QComboBox()
        self.language.addItem("自动检测", "auto")
        self.language.addItem("中文", "zh")
        self.language.addItem("英文", "en")
        self.language.setCurrentIndex(max(0, self.language.findData(config.language)))
        self.stream_response = QCheckBox("模型支持时使用流式文字返回（MiMo 支持）")
        self.stream_response.setChecked(config.stream_response)

        self.capture_mic = QCheckBox("转录麦克风（我）")
        self.capture_mic.setChecked(config.capture_mic)
        self.capture_system = QCheckBox("转录电脑声音（会议/视频）")
        self.capture_system.setChecked(config.capture_system)
        self.compat_mic = QCheckBox(
            "麦克风兼容模式：优先使用 MME/DirectSound，避免触发通话模式"
        )
        self.compat_mic.setChecked(config.mic_compat_mode)
        self.prevent_ducking = QCheckBox(
            "防止 Windows 在麦克风启用时降低或静音其他应用声音（推荐）"
        )
        self.prevent_ducking.setChecked(config.prevent_windows_ducking)

        self.mic_combo = QComboBox()
        self.system_combo = QComboBox()
        self.refresh_button = QPushButton("刷新设备")
        self.refresh_button.clicked.connect(self.refresh_devices)

        self.segmentation = QComboBox()
        self.segmentation.addItem("智能语音分片（推荐）", "vad")
        self.segmentation.addItem("固定时长（兼容模式）", "fixed")
        self.segmentation.setCurrentIndex(
            max(0, self.segmentation.findData(config.segmentation_mode))
        )
        self.segmentation.currentIndexChanged.connect(self.on_segmentation_changed)

        self.vad_mode = QComboBox()
        self.vad_mode.addItem("0 - 最宽松，保留更多声音", 0)
        self.vad_mode.addItem("1 - 较宽松", 1)
        self.vad_mode.addItem("2 - 平衡（推荐）", 2)
        self.vad_mode.addItem("3 - 最严格，过滤更多噪声", 3)
        self.vad_mode.setCurrentIndex(max(0, self.vad_mode.findData(config.vad_mode)))

        self.end_pause = QComboBox()
        for value in (350, 450, 600, 750, 900, 1200, 1600):
            self.end_pause.addItem(f"{value} 毫秒", value)
        pause_index = self.end_pause.findData(config.speech_end_ms)
        if pause_index < 0:
            self.end_pause.addItem(f"{config.speech_end_ms} 毫秒", config.speech_end_ms)
            pause_index = self.end_pause.count() - 1
        self.end_pause.setCurrentIndex(pause_index)

        self.max_segment = QComboBox()
        for value in (6.0, 8.0, 10.0, 12.0, 15.0, 20.0, 30.0):
            self.max_segment.addItem(f"{value:g} 秒", value)
        max_index = self.max_segment.findData(float(config.max_segment_seconds))
        if max_index < 0:
            self.max_segment.addItem(
                f"{config.max_segment_seconds:g} 秒", float(config.max_segment_seconds)
            )
            max_index = self.max_segment.count() - 1
        self.max_segment.setCurrentIndex(max_index)

        self.chunk = QComboBox()
        for value in (3.0, 4.0, 5.0, 6.0, 8.0):
            self.chunk.addItem(f"{value:.0f} 秒", value)
        self.chunk.setCurrentIndex(max(0, self.chunk.findData(config.chunk_seconds)))

        self.alpha = QSlider(Qt.Horizontal)
        self.alpha.setRange(80, 245)
        self.alpha.setValue(config.panel_alpha)

        mic_row = QHBoxLayout()
        mic_row.addWidget(self.mic_combo, 1)
        system_row = QHBoxLayout()
        system_row.addWidget(self.system_combo, 1)
        system_row.addWidget(self.refresh_button)

        form = QFormLayout()
        form.addRow("API 平台", self.provider)
        form.addRow("识别模型", self.model)
        form.addRow("MiMo API Key", self.mimo_api_key)
        form.addRow("SiliconFlow API Key", self.siliconflow_api_key)
        form.addRow("SiliconFlow 地址", self.siliconflow_base_url)
        form.addRow("识别语言", self.language)
        form.addRow("增量字幕", self.stream_response)
        form.addRow("采集内容", self.capture_mic)
        form.addRow("", self.capture_system)
        form.addRow("麦克风设备", mic_row)
        form.addRow("电脑声音设备", system_row)
        form.addRow("音频兼容", self.compat_mic)
        form.addRow("Windows 通讯音量", self.prevent_ducking)
        form.addRow("分片方式", self.segmentation)
        form.addRow("VAD 灵敏度", self.vad_mode)
        form.addRow("结束停顿", self.end_pause)
        form.addRow("最长语音段", self.max_segment)
        form.addRow("固定分片长度", self.chunk)
        form.addRow("面板透明度", self.alpha)

        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color:#aaaaaa;font-size:12px;")

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.note)
        layout.addWidget(buttons)
        self.on_provider_changed()
        self.on_segmentation_changed()
        self.refresh_devices()

    def on_provider_changed(self, *_: Any) -> None:
        provider = str(self.provider.currentData() or "mimo")
        current_text = self.model.currentText().strip()
        if current_text and self._current_provider in self._model_values:
            self._model_values[self._current_provider] = current_text
        self.model.blockSignals(True)
        self.model.clear()
        if provider == "siliconflow":
            self.model.addItems(list(SILICONFLOW_MODELS))
            target = self._model_values["siliconflow"]
            self.language.setEnabled(False)
            self.mimo_api_key.setEnabled(False)
            self.siliconflow_api_key.setEnabled(True)
            self.siliconflow_base_url.setEnabled(True)
            self.stream_response.setEnabled(False)
            self.note.setText(
                "SiliconFlow 当前语音转文字接口返回一次性 text 字段，未公开流式转写参数；"
                "智能 VAD 仍会在检测到停顿后立即上传，从而减少等待。"
            )
        else:
            self.model.addItem("mimo-v2.5-asr")
            target = self._model_values["mimo"]
            self.language.setEnabled(True)
            self.mimo_api_key.setEnabled(True)
            self.siliconflow_api_key.setEnabled(False)
            self.siliconflow_base_url.setEnabled(False)
            self.stream_response.setEnabled(True)
            self.note.setText(
                "MiMo 支持对已上传语音段进行流式文字响应。智能 VAD 会缓存开头、在停顿后立即提交；"
                "这不是持续上传音频的 WebSocket，长时间不停顿时会按最长语音段切分。"
            )
        index = self.model.findText(target)
        if index >= 0:
            self.model.setCurrentIndex(index)
        else:
            self.model.setEditText(target)
        self.model.blockSignals(False)
        self._current_provider = provider

    def on_segmentation_changed(self, *_: Any) -> None:
        adaptive = self.segmentation.currentData() == "vad"
        self.vad_mode.setEnabled(adaptive)
        self.end_pause.setEnabled(adaptive)
        self.max_segment.setEnabled(adaptive)
        self.chunk.setEnabled(not adaptive)

    def refresh_devices(self) -> None:
        self.mic_combo.clear()
        self.system_combo.clear()
        try:
            self.inventory = scan_audio_devices()
        except Exception as exc:
            logging.exception("Device refresh failed")
            QMessageBox.critical(self, "设备扫描失败", str(exc))
            return

        for device in sorted(
            self.inventory.microphones, key=lambda d: (_host_rank(d.host_api), d.name)
        ):
            self.mic_combo.addItem(device.display_name, device.key)
        for device in self.inventory.loopbacks:
            self.system_combo.addItem(device.display_name, device.key)

        mic_index = self.mic_combo.findData(self.config.mic_device_key)
        if mic_index < 0:
            resolved = resolve_microphone(
                self.inventory, self.config.mic_device_key, self.config.mic_compat_mode
            )
            mic_index = self.mic_combo.findData(resolved.key) if resolved else -1
        self.mic_combo.setCurrentIndex(max(0, mic_index))

        system_index = self.system_combo.findData(self.config.system_device_key)
        if system_index < 0:
            resolved_system = resolve_loopback(
                self.inventory, self.config.system_device_key
            )
            system_index = (
                self.system_combo.findData(resolved_system.key)
                if resolved_system
                else -1
            )
        self.system_combo.setCurrentIndex(max(0, system_index))

    def accept(self) -> None:
        provider = str(self.provider.currentData() or "mimo")
        mimo_key = self.mimo_api_key.text().strip()
        siliconflow_key = self.siliconflow_api_key.text().strip()
        selected_key = siliconflow_key if provider == "siliconflow" else mimo_key
        env_name = "SILICONFLOW_API_KEY" if provider == "siliconflow" else "MIMO_API_KEY"
        if not selected_key and not os.getenv(env_name) and not load_api_key(provider):
            QMessageBox.warning(
                self,
                "缺少 API Key",
                f"请填写 {provider_display_name(provider)} API Key。",
            )
            return
        try:
            if mimo_key:
                save_api_key("mimo", mimo_key)
            if siliconflow_key:
                save_api_key("siliconflow", siliconflow_key)
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", f"无法保存 API Key：{exc}")
            return

        model = self.model.currentText().strip()
        if not model:
            QMessageBox.warning(self, "缺少模型", "请填写识别模型 ID。")
            return
        base_url = self.siliconflow_base_url.text().strip().rstrip("/")
        if provider == "siliconflow" and not base_url.startswith(("https://", "http://")):
            QMessageBox.warning(
                self, "地址无效", "SiliconFlow 地址必须以 http:// 或 https:// 开头。"
            )
            return

        self.config.api_provider = provider
        if provider == "siliconflow":
            self.config.siliconflow_model = model
        else:
            self.config.mimo_model = model
        self.config.siliconflow_base_url = base_url or DEFAULT_SILICONFLOW_BASE_URL
        self.config.language = str(self.language.currentData())
        self.config.stream_response = self.stream_response.isChecked()
        self.config.capture_mic = self.capture_mic.isChecked()
        self.config.capture_system = self.capture_system.isChecked()
        self.config.mic_compat_mode = self.compat_mic.isChecked()
        self.config.prevent_windows_ducking = self.prevent_ducking.isChecked()
        self.config.mic_device_key = str(self.mic_combo.currentData() or "")
        self.config.system_device_key = str(self.system_combo.currentData() or "")
        self.config.segmentation_mode = str(self.segmentation.currentData() or "vad")
        self.config.vad_mode = int(self.vad_mode.currentData())
        self.config.speech_end_ms = int(self.end_pause.currentData())
        self.config.max_segment_seconds = float(self.max_segment.currentData())
        self.config.chunk_seconds = float(self.chunk.currentData())
        self.config.panel_alpha = int(self.alpha.value())
        self.config.save()
        super().accept()


class HeaderBar(QFrame):
    drag_moved = Signal(QPoint)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._drag_origin: Optional[QPoint] = None
        self.setObjectName("header")

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            self._drag_origin = event.globalPosition().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._drag_origin is not None and event.buttons() & Qt.LeftButton:
            current = event.globalPosition().toPoint()
            delta = current - self._drag_origin
            self._drag_origin = current
            self.drag_moved.emit(delta)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        self._drag_origin = None
        super().mouseReleaseEvent(event)


# -------------------------------- main window --------------------------------

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.config = AppConfig.load()
        self.audio_engine: Optional[AudioEngine] = None
        self.api_workers: dict[str, ApiWorker] = {}
        self.api_signals = ApiSignals()
        self.api_signals.partial.connect(self.on_partial_transcript)
        self.api_signals.result.connect(self.on_transcript)
        self.api_signals.failed.connect(self.on_api_error)
        self.api_signals.queue_size.connect(self.on_queue_size)
        self.api_signals.validation.connect(self.on_api_validation)
        self.previous_text = {"mic": "", "system": ""}
        self.partial_cursors: dict[str, QTextCursor] = {}
        self.vad_states = {"mic": "idle", "system": "idle"}
        self.queue_counts = {"mic": 0, "system": 0}
        self.running = False
        self.closing = False

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(860, 390)
        self.setMinimumSize(520, 250)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._build_ui()
        self._install_shortcuts()
        self.apply_style()
        self.append_system_message(
            "0.6.0：智能 VAD 在停顿后立即提交；MiMo 支持增量显示流式文字。"
        )

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)

        self.header = HeaderBar()
        self.header.drag_moved.connect(lambda delta: self.move(self.pos() + delta))
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(12, 7, 8, 7)
        self.title_label = QLabel("多模型实时字幕")
        self.status_label = QLabel("未启动")
        self.status_label.setObjectName("status")
        self.start_button = QPushButton("开始")
        self.start_button.clicked.connect(self.toggle_running)
        self.clear_button = QPushButton("清空")
        self.clear_button.clicked.connect(self.clear_text)
        self.settings_button = QPushButton("设置")
        self.settings_button.clicked.connect(self.open_settings)
        self.min_button = QPushButton("—")
        self.min_button.setFixedWidth(34)
        self.min_button.clicked.connect(self.showMinimized)
        self.close_button = QPushButton("×")
        self.close_button.setFixedWidth(34)
        self.close_button.clicked.connect(self.close)
        header_layout.addWidget(self.title_label)
        header_layout.addWidget(self.status_label)
        header_layout.addStretch(1)
        for widget in (
            self.start_button,
            self.clear_button,
            self.settings_button,
            self.min_button,
            self.close_button,
        ):
            header_layout.addWidget(widget)

        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setAcceptRichText(True)
        self.text.setTextInteractionFlags(
            Qt.TextSelectableByMouse
            | Qt.TextSelectableByKeyboard
            | Qt.LinksAccessibleByMouse
        )

        meters = QFrame()
        meter_layout = QHBoxLayout(meters)
        meter_layout.setContentsMargins(12, 4, 8, 2)
        self.mic_meter = QProgressBar()
        self.system_meter = QProgressBar()
        for meter in (self.mic_meter, self.system_meter):
            meter.setRange(0, 100)
            meter.setValue(0)
            meter.setTextVisible(False)
            meter.setMaximumHeight(10)
        meter_layout.addWidget(QLabel("麦"))
        meter_layout.addWidget(self.mic_meter, 1)
        meter_layout.addSpacing(12)
        meter_layout.addWidget(QLabel("电脑"))
        meter_layout.addWidget(self.system_meter, 1)

        footer = QFrame()
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(12, 2, 5, 6)
        self.device_label = QLabel("等待音频设备")
        self.api_label = QLabel("API: 未检测")
        self.queue_label = QLabel("")
        footer_layout.addWidget(self.device_label, 1)
        footer_layout.addWidget(self.api_label)
        footer_layout.addWidget(self.queue_label)
        footer_layout.addWidget(QSizeGrip(self))

        outer.addWidget(self.header)
        outer.addWidget(self.text, 1)
        outer.addWidget(meters)
        outer.addWidget(footer)
        self.setCentralWidget(root)

    def _install_shortcuts(self) -> None:
        for shortcut, callback in (
            ("Ctrl+L", self.clear_text),
            ("Ctrl+Shift+C", self.copy_all),
            ("Ctrl+Space", self.toggle_running),
        ):
            action = QAction(self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(callback)
            self.addAction(action)

    def apply_style(self) -> None:
        alpha = self.config.panel_alpha
        self.setStyleSheet(
            f"""
            QWidget#root {{
                background-color: rgba(20, 22, 27, {alpha});
                border: 1px solid rgba(255,255,255,55);
                border-radius: 10px;
            }}
            QFrame#header {{
                background-color: rgba(42, 45, 53, {min(245, alpha + 20)});
                border-top-left-radius: 10px;
                border-top-right-radius: 10px;
            }}
            QLabel {{ color: #f4f4f4; }}
            QLabel#status {{ color: #9ed7ff; padding-left: 10px; }}
            QTextEdit {{
                background: transparent; color: #ffffff; border: none; padding: 12px;
                selection-background-color: rgba(70,150,255,180);
                font-size: {self.config.font_size}px;
            }}
            QProgressBar {{
                border: 1px solid rgba(255,255,255,45); border-radius: 4px;
                background: rgba(255,255,255,18);
            }}
            QProgressBar::chunk {{ background: rgba(95,195,255,190); border-radius: 3px; }}
            QPushButton {{
                color: #ffffff; background-color: rgba(255,255,255,25);
                border: 1px solid rgba(255,255,255,40); border-radius: 5px;
                padding: 4px 10px;
            }}
            QPushButton:hover {{ background-color: rgba(255,255,255,55); }}
            QPushButton:pressed {{ background-color: rgba(255,255,255,75); }}
            """
        )
        self.text.setFont(QFont("Microsoft YaHei UI", self.config.font_size))

    def toggle_running(self) -> None:
        if self.running:
            self.stop_recognition()
        else:
            self.start_recognition()

    def _maybe_fix_ducking(self) -> bool:
        if not self.config.prevent_windows_ducking:
            return True
        current = get_ducking_preference()
        if current == 3:
            return True
        if not self.config.ducking_prompt_answered:
            box = QMessageBox(self)
            box.setWindowTitle("防止电脑声音被静音")
            box.setIcon(QMessageBox.Information)
            box.setText(
                "Windows 可能在麦克风启用时降低或静音视频/会议声音。\n\n"
                "是否把当前用户的“通讯”设置改为“不执行任何操作”？"
            )
            apply_button = box.addButton("应用推荐设置", QMessageBox.AcceptRole)
            box.addButton("保持当前设置", QMessageBox.RejectRole)
            box.exec()
            self.config.ducking_prompt_answered = True
            if box.clickedButton() is apply_button:
                try:
                    set_ducking_do_nothing()
                except Exception as exc:
                    logging.exception("Failed setting ducking preference")
                    QMessageBox.warning(self, "设置失败", str(exc))
            self.config.save()
        return True

    def start_recognition(self) -> None:
        provider = self.config.api_provider
        api_key = load_api_key(provider)
        if not api_key:
            dialog = SettingsDialog(self.config, self)
            if dialog.exec() != QDialog.Accepted:
                return
            self.config = AppConfig.load()
            provider = self.config.api_provider
            api_key = load_api_key(provider)
        if not api_key:
            QMessageBox.warning(
                self,
                "缺少 API Key",
                f"未找到 {provider_display_name(provider)} API Key。",
            )
            return
        if not self.config.capture_mic and not self.config.capture_system:
            QMessageBox.warning(self, "未选择音源", "请至少启用麦克风或电脑声音。")
            return

        self._maybe_fix_ducking()
        try:
            inventory = scan_audio_devices()
        except Exception as exc:
            QMessageBox.critical(self, "设备扫描失败", str(exc))
            return

        mic_device = (
            resolve_microphone(
                inventory, self.config.mic_device_key, self.config.mic_compat_mode
            )
            if self.config.capture_mic
            else None
        )
        system_device = (
            resolve_loopback(inventory, self.config.system_device_key)
            if self.config.capture_system
            else None
        )
        if self.config.capture_mic and mic_device is None:
            QMessageBox.warning(self, "麦克风不可用", "没有找到可用麦克风。")
        if self.config.capture_system and system_device is None:
            QMessageBox.warning(
                self,
                "电脑声音不可用",
                "没有找到 WASAPI Loopback。麦克风仍可单独工作。",
            )
        if mic_device is None and system_device is None:
            return

        self.running = True
        self.start_button.setText("停止")
        self.settings_button.setEnabled(False)
        self.status_label.setText(
            "等待说话" if self.config.segmentation_mode == "vad" else "正在监听"
        )
        provider_name = provider_display_name(self.config.api_provider)
        active_model = (
            self.config.siliconflow_model
            if self.config.api_provider == "siliconflow"
            else self.config.mimo_model
        )
        self.api_label.setText(f"{provider_name}: 检测中")
        logging.info(
            "Starting recognition provider=%s model=%s",
            self.config.api_provider,
            active_model,
        )
        self.previous_text = {"mic": "", "system": ""}
        self.partial_cursors.clear()
        self.vad_states = {"mic": "idle", "system": "idle"}
        self.queue_counts = {"mic": 0, "system": 0}

        self.api_workers.clear()
        for source in ("mic", "system"):
            enabled = (source == "mic" and mic_device is not None) or (
                source == "system" and system_device is not None
            )
            if enabled:
                worker = ApiWorker(source, api_key, self.config, self.api_signals)
                worker.start()
                self.api_workers[source] = worker

        threading.Thread(
            target=self._validate_api,
            args=(api_key,),
            name=f"ASR-{self.config.api_provider}-Validate",
            daemon=True,
        ).start()

        self.audio_engine = AudioEngine(
            mic_device=mic_device,
            system_device=system_device,
            config=self.config,
        )
        self.audio_engine.chunk_ready.connect(self.on_audio_chunk)
        self.audio_engine.level_changed.connect(self.on_level)
        self.audio_engine.device_opened.connect(self.on_device_opened)
        self.audio_engine.vad_state_changed.connect(self.on_vad_state)
        self.audio_engine.failed.connect(self.on_audio_error)
        self.audio_engine.engine_stopped.connect(self.on_engine_stopped)
        self.audio_engine.start()

    def _validate_api(self, api_key: str) -> None:
        try:
            create_asr_client(self.config, api_key).validate()
            self.api_signals.validation.emit(True, "已连接")
        except Exception as exc:
            logging.exception("API validation failed")
            self.api_signals.validation.emit(False, str(exc))

    def stop_recognition(self) -> None:
        if not self.running and self.audio_engine is None:
            return
        self.running = False
        self.start_button.setEnabled(False)
        self.status_label.setText("正在停止……")
        for worker in self.api_workers.values():
            worker.stop()
        self.api_workers.clear()

        engine = self.audio_engine
        if engine is not None:
            engine.request_stop()
            if not engine.wait(5000):
                logging.error("Audio engine did not stop within 5 seconds")
                self.status_label.setText("正在释放音频设备")
                QTimer.singleShot(500, self._finish_stop_when_ready)
                return
        self._complete_stop()
        if self.closing:
            QTimer.singleShot(0, self.close)

    def _finish_stop_when_ready(self) -> None:
        engine = self.audio_engine
        if engine is not None and engine.isRunning():
            QTimer.singleShot(500, self._finish_stop_when_ready)
            return
        self._complete_stop()
        if self.closing:
            self.close()

    def _complete_stop(self) -> None:
        self.audio_engine = None
        self.start_button.setEnabled(True)
        self.start_button.setText("开始")
        self.settings_button.setEnabled(True)
        self.status_label.setText("已停止")
        self.queue_label.setText("")
        self.mic_meter.setValue(0)
        self.system_meter.setValue(0)

    def on_engine_stopped(self) -> None:
        logging.info("Received clean audio engine stop signal")
        if self.running:
            # The engine ended unexpectedly (for example, both devices failed).
            self.running = False
            for worker in self.api_workers.values():
                worker.stop()
            self.api_workers.clear()
            self._complete_stop()

    def on_audio_chunk(self, source: str, wav_bytes: bytes, timestamp: float) -> None:
        worker = self.api_workers.get(source)
        if worker:
            worker.submit(wav_bytes, timestamp)
            provider_name = provider_display_name(self.config.api_provider)
            state = "正在识别麦克风" if source == "mic" else "正在识别电脑声音"
            self.api_label.setText(f"{provider_name}: {state}")

    def on_level(self, source: str, percent: int, raw_rms: int) -> None:
        del raw_rms
        if source == "mic":
            self.mic_meter.setValue(percent)
        else:
            self.system_meter.setValue(percent)

    def on_device_opened(self, source: str, description: str) -> None:
        label = "麦克风" if source == "mic" else "电脑"
        current = self.device_label.text()
        entry = f"{label}: {description}"
        if current == "等待音频设备" or current.startswith("正在连接"):
            self.device_label.setText(entry)
        elif entry not in current:
            self.device_label.setText(current + " | " + entry)

    def on_audio_error(self, source: str, message: str) -> None:
        label = {"mic": "麦克风", "system": "电脑声音", "engine": "音频引擎"}.get(
            source, source
        )
        self.append_system_message(f"⚠ {label}采集失败：{message}")
        logging.error("Audio error source=%s message=%s", source, message)

    def on_vad_state(self, source: str, state: str) -> None:
        self.vad_states[source] = state
        if not self.running or self.config.segmentation_mode != "vad":
            return
        speaking = [
            "麦克风" if item == "mic" else "电脑"
            for item, value in self.vad_states.items()
            if value == "speech"
        ]
        if speaking:
            self.status_label.setText("正在收音：" + " + ".join(speaking))
        elif state == "sent":
            self.status_label.setText("检测到停顿，已提交语音段")
        else:
            self.status_label.setText("等待说话")

    @staticmethod
    def _transcript_html(
        source: str, timestamp: float, text: str, *, partial: bool
    ) -> str:
        time_text = datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")
        label, color = ("我", "#75d69c") if source == "mic" else ("会议", "#7fc8ff")
        suffix = ' <span style="color:#888888">…</span>' if partial else ""
        return (
            f'<span style="color:#888888">{time_text}</span> '
            f'<b style="color:{color}">[{label}]</b> '
            f'<span style="color:#ffffff">{html.escape(text)}</span>{suffix}'
        )

    def _scroll_to_end_if_unselected(self) -> None:
        cursor = self.text.textCursor()
        if cursor.hasSelection():
            return
        cursor.movePosition(QTextCursor.End)
        self.text.setTextCursor(cursor)

    def _upsert_partial_line(
        self, source: str, timestamp: float, text: str, *, partial: bool
    ) -> None:
        line_html = self._transcript_html(source, timestamp, text, partial=partial)
        stored = self.partial_cursors.get(source)
        if stored is None or stored.document() is None:
            cursor = QTextCursor(self.text.document())
            cursor.movePosition(QTextCursor.End)
            if not self.text.document().isEmpty():
                cursor.insertBlock()
            start = cursor.position()
            cursor.insertHtml(line_html)
            end = cursor.position()
            selected = QTextCursor(self.text.document())
            selected.setPosition(start)
            selected.setPosition(end, QTextCursor.KeepAnchor)
            self.partial_cursors[source] = selected
            self._scroll_to_end_if_unselected()
            return

        start = stored.selectionStart()
        stored.beginEditBlock()
        stored.removeSelectedText()
        stored.insertHtml(line_html)
        end = stored.position()
        stored.setPosition(start)
        stored.setPosition(end, QTextCursor.KeepAnchor)
        stored.endEditBlock()
        self.partial_cursors[source] = stored
        self._scroll_to_end_if_unselected()

    def _discard_partial(self, source: str) -> None:
        cursor = self.partial_cursors.pop(source, None)
        if cursor is not None and cursor.document() is not None:
            cursor.removeSelectedText()

    def on_partial_transcript(self, source: str, timestamp: float, text: str) -> None:
        cleaned = remove_overlap(self.previous_text.get(source, ""), text)
        if not cleaned:
            return
        self._upsert_partial_line(source, timestamp, cleaned, partial=True)
        self.api_label.setText(
            f"{provider_display_name(self.config.api_provider)}: 流式返回中"
        )

    def on_transcript(self, source: str, timestamp: float, text: str) -> None:
        cleaned = remove_overlap(self.previous_text.get(source, ""), text)
        self.previous_text[source] = text
        if not cleaned:
            self._discard_partial(source)
            return
        if source in self.partial_cursors:
            self._upsert_partial_line(source, timestamp, cleaned, partial=False)
            self.partial_cursors.pop(source, None)
        else:
            self.text.append(self._transcript_html(source, timestamp, cleaned, partial=False))
            self._scroll_to_end_if_unselected()
        self.api_label.setText(
            f"{provider_display_name(self.config.api_provider)}: 正常"
        )
        if self.config.segmentation_mode == "vad":
            self.status_label.setText("等待说话")
        else:
            self.status_label.setText("正在监听")

    def on_api_error(self, source: str, message: str) -> None:
        self._discard_partial(source)
        label = "我" if source == "mic" else "会议"
        self.append_system_message(f"⚠ [{label}] 转写失败：{message}")
        self.api_label.setText(
            f"{provider_display_name(self.config.api_provider)}: 错误"
        )

    def on_api_validation(self, ok: bool, message: str) -> None:
        provider_name = provider_display_name(self.config.api_provider)
        self.api_label.setText(
            f"{provider_name}: 已连接" if ok else f"{provider_name}: 检测失败"
        )
        if not ok:
            self.append_system_message(f"⚠ API 检测失败：{message}")

    def on_queue_size(self, source: str, size: int) -> None:
        self.queue_counts[source] = size
        parts = []
        if self.queue_counts["mic"]:
            parts.append(f"麦 {self.queue_counts['mic']}")
        if self.queue_counts["system"]:
            parts.append(f"电脑 {self.queue_counts['system']}")
        self.queue_label.setText("待处理 " + "/".join(parts) if parts else "")

    def append_system_message(self, message: str) -> None:
        self.text.append(f'<span style="color:#aaaaaa">{html.escape(message)}</span>')

    def open_settings(self) -> None:
        if self.running:
            return
        dialog = SettingsDialog(self.config, self)
        if dialog.exec() == QDialog.Accepted:
            self.config = AppConfig.load()
            self.apply_style()

    def clear_text(self) -> None:
        self.text.clear()
        self.partial_cursors.clear()
        self.previous_text = {"mic": "", "system": ""}

    def copy_all(self) -> None:
        QApplication.clipboard().setText(self.text.toPlainText())
        self.status_label.setText("已复制全部")

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self.audio_engine is not None and self.audio_engine.isRunning():
            self.closing = True
            event.ignore()
            self.stop_recognition()
            return
        event.accept()


def main() -> int:
    setup_logging()
    logging.info(
        "Starting %s %s on %s",
        APP_NAME,
        APP_VERSION,
        sys.version.replace("\n", " "),
    )
    if sys.platform != "win32":
        logging.error("Windows only")
        print("This application currently supports Windows only.", file=sys.stderr)
        return 1

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    palette = app.palette()
    palette.setColor(QPalette.Window, QColor(30, 30, 30))
    palette.setColor(QPalette.WindowText, QColor(240, 240, 240))
    palette.setColor(QPalette.Base, QColor(35, 35, 35))
    palette.setColor(QPalette.Text, QColor(240, 240, 240))
    app.setPalette(palette)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
