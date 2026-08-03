from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tarfile
import time

import requests

MODELS = {
    "accurate": {
        "name": "sherpa-onnx-streaming-paraformer-bilingual-zh-en",
        "required": ("tokens.txt", "encoder.int8.onnx", "decoder.int8.onnx"),
        "description": "较高准确率双语 Paraformer（推荐，约 226 MB int8 模型）",
    },
    "fast": {
        "name": "sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16",
        "required": (
            "tokens.txt",
            "encoder-epoch-99-avg-1.int8.onnx",
            "decoder-epoch-99-avg-1.onnx",
            "joiner-epoch-99-avg-1.int8.onnx",
        ),
        "description": "低资源小型 Zipformer",
    },
}
DEFAULT_PROXY = "http://127.0.0.1:20800"


def project_dir() -> Path:
    return Path(__file__).resolve().parent


def app_data_dir() -> Path:
    root = os.getenv("APPDATA")
    if root:
        return Path(root) / "MiMoLiveCaption"
    return Path.home() / ".mimo_live_caption"


def normalize_proxy(value: str) -> str:
    value = value.strip()
    if value and "://" not in value:
        value = "http://" + value
    return value


def configured_proxy() -> str:
    path = app_data_dir() / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not data.get("network_proxy_enabled"):
        return ""
    return normalize_proxy(str(data.get("network_proxy_url") or ""))


def resolve_proxy(cli_proxy: str | None, direct: bool) -> str:
    if direct:
        return ""
    candidates = (
        cli_proxy or "",
        os.getenv("MIMO_DOWNLOAD_PROXY", ""),
        configured_proxy(),
        DEFAULT_PROXY,
    )
    for candidate in candidates:
        value = normalize_proxy(candidate)
        if value:
            return value
    return ""


def validate(target: Path, required: tuple[str, ...]) -> bool:
    return all((target / name).is_file() for name in required)


def safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
    destination = destination.resolve()
    for member in archive.getmembers():
        member_path = (destination / member.name).resolve()
        if destination != member_path and destination not in member_path.parents:
            raise RuntimeError(f"压缩包包含不安全路径：{member.name}")
    try:
        archive.extractall(destination, filter="data")
    except TypeError:
        archive.extractall(destination)


def download(url: str, archive: Path, proxy: str) -> None:
    proxies = {"http": proxy, "https": proxy} if proxy else None
    part = archive.with_suffix(archive.suffix + ".part")
    existing = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": "MiMoLiveCaption/0.9.2"}
    if existing:
        headers["Range"] = f"bytes={existing}-"

    with requests.get(
        url,
        stream=True,
        timeout=(20, 180),
        proxies=proxies,
        headers=headers,
    ) as response:
        append = existing > 0 and response.status_code == 206
        if response.status_code == 416 and existing:
            part.replace(archive)
            return
        response.raise_for_status()
        if not append:
            existing = 0
        total_remaining = int(response.headers.get("content-length") or 0)
        total = existing + total_remaining if total_remaining else 0
        downloaded = existing
        started = time.monotonic()
        mode = "ab" if append else "wb"
        with part.open(mode) as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                output.write(chunk)
                downloaded += len(chunk)
                elapsed = max(0.1, time.monotonic() - started)
                speed = max(0, downloaded - existing) / elapsed / 1024 / 1024
                if total:
                    percent = downloaded * 100 / total
                    status = f"下载 {percent:5.1f}%  {downloaded/1024/1024:,.1f} MB"
                else:
                    status = f"下载 {downloaded/1024/1024:,.1f} MB"
                print(f"\r{status}  {speed:,.1f} MB/s", end="", flush=True)
        print()
    part.replace(archive)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="通过代理下载 sherpa-onnx 流式模型到当前项目的 models 文件夹。"
    )
    parser.add_argument(
        "--model",
        choices=tuple(MODELS),
        default="accurate",
        help="accurate=较高准确率 Paraformer；fast=低资源小模型",
    )
    parser.add_argument(
        "--proxy",
        help="代理地址，例如 http://127.0.0.1:20800 或 socks5h://127.0.0.1:20800",
    )
    parser.add_argument("--direct", action="store_true", help="忽略代理并直连下载")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    spec = MODELS[args.model]
    model_name = str(spec["name"])
    required = tuple(spec["required"])
    model_url = (
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
        + model_name
        + ".tar.bz2"
    )
    models_dir = project_dir() / "models"
    target = models_dir / model_name
    models_dir.mkdir(parents=True, exist_ok=True)

    print(f"项目目录：{project_dir()}")
    print(f"模型类型：{spec['description']}")
    print(f"模型目录：{target}")
    if validate(target, required):
        print("模型已经安装且文件完整。")
        return 0

    proxy = resolve_proxy(args.proxy, args.direct)
    print(f"下载地址：{model_url}")
    print(f"网络路径：{proxy or '直连'}")
    archive = models_dir / f"{model_name}.tar.bz2"

    try:
        download(model_url, archive, proxy)
        print("正在解压……")
        with tarfile.open(archive, "r:bz2") as tar:
            safe_extract(tar, models_dir)
        archive.unlink(missing_ok=True)
    except Exception as exc:
        print(f"模型安装失败：{exc}", file=sys.stderr)
        print(
            "请确认本地代理端口可用。HTTP 示例：http://127.0.0.1:20800；"
            "SOCKS5 示例：socks5h://127.0.0.1:20800。",
            file=sys.stderr,
        )
        return 1

    if not validate(target, required):
        print(f"解压完成，但模型文件不完整：{target}", file=sys.stderr)
        return 2
    print(f"模型安装完成：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
