from __future__ import annotations

import ctypes
import gzip
import hashlib
import json
import math
import os
import platform
import queue
import re
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections import Counter, defaultdict, deque
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:
    tk = None
    ttk = None

APP_NAME = "ArtOfWar3"
APP_TITLE = "Art Of War 3 控制器"
SCRIPT_NAME = "ArtOfWar3.py"
REQUIRED_PYTHON = (3, 12, 10)
WINDOWS_11_MIN_BUILD = 22000
CONFIG_VERSION = 1
MODEL_VERSION = 1
STATE_BITS = 64
CONFIG_MAX_BYTES = 1024 * 1024
MANIFEST_MAX_BYTES = 2 * 1024 * 1024
MODEL_MAX_COMPRESSED_BYTES = 64 * 1024 * 1024
MODEL_MAX_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
EXPERIENCE_LINE_MAX_CHARS = 4096
HEX_STATE_RE = re.compile(r"^[0-9a-f]{16}$")
ACTION_RE = re.compile(r"^(?:K:[0-9A-F]{2}|M:[LRM]:\d{1,3}:\d{1,3})$")


class ModeCancelled(Exception):
    pass


def check_cancel(stop_event: threading.Event | None) -> None:
    if stop_event is not None and stop_event.is_set():
        raise ModeCancelled


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path, stop_event: threading.Event | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            check_cancel(stop_event)
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_lines(path: Path, lines, stop_event: threading.Event | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            for index, line in enumerate(lines, 1):
                if index % 256 == 0:
                    check_cancel(stop_event)
                stream.write(line)
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        check_cancel(stop_event)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def default_config() -> dict:
    return {
        "version": CONFIG_VERSION,
        "manifest_url": "",
        "game_command": [],
        "window_title_keywords": [
            "Art of War 3",
            "Art Of War 3",
            "战争艺术3",
            "全球冲突",
        ],
        "capture_interval_seconds": 0.20,
        "ai_action_cooldown_seconds": 0.45,
        "ai_repeat_cooldown_seconds": 1.20,
        "ai_max_hamming_distance": 18,
        "max_experience_events": 120000,
        "max_model_states": 30000,
        "remote_file_limit_mb": 512,
    }


def get_desktop() -> Path:
    if os.name == "nt":
        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _ = winreg.QueryValueEx(key, "Desktop")
            return Path(os.path.expandvars(value)).resolve()
        except Exception:
            pass
    return (Path.home() / "Desktop").resolve()


DESKTOP = get_desktop()
DATA_ROOT = DESKTOP / APP_NAME
CONFIG_PATH = DATA_ROOT / "config.json"
EXPERIENCE_PATH = DATA_ROOT / "experience" / "events.jsonl"
MODEL_PATH = DATA_ROOT / "model" / "policy.json.gz"
INTEGRITY_PATH = DATA_ROOT / "state" / "integrity.json"
LOG_PATH = DATA_ROOT / "logs" / "ArtOfWar3.log"
SCRIPT_COPY_PATH = DATA_ROOT / SCRIPT_NAME


def ensure_layout() -> bool:
    try:
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        for relative in ("experience", "model", "state", "logs", "downloads"):
            (DATA_ROOT / relative).mkdir(parents=True, exist_ok=True)
        current_script = Path(__file__).resolve()
        target_script = SCRIPT_COPY_PATH.resolve()
        if current_script == target_script:
            return True
        source_hash = sha256_file(current_script)
        if not target_script.is_file() or sha256_file(target_script) != source_hash:
            atomic_write_bytes(target_script, current_script.read_bytes())
        return target_script.is_file() and sha256_file(target_script) == source_hash
    except OSError as error:
        log(f"script synchronization failed: {error}")
        return False


def log(message: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as stream:
            stream.write(f"{utc_now()} {message}\n")
    except OSError:
        pass


def _number(value: object, minimum: float, maximum: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and minimum <= float(value) <= maximum
    )


def _integer(value: object, minimum: int, maximum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum


def _valid_config_field(name: str, value: object) -> bool:
    if name == "manifest_url":
        return isinstance(value, str) and (not value.strip() or value.strip().lower().startswith("https://"))
    if name == "game_command":
        return (
            isinstance(value, list)
            and len(value) <= 64
            and all(isinstance(item, str) and item and len(item) <= 32767 for item in value)
        )
    if name == "window_title_keywords":
        return (
            isinstance(value, list)
            and 1 <= len(value) <= 32
            and all(isinstance(item, str) and item.strip() and len(item) <= 128 for item in value)
        )
    if name == "capture_interval_seconds":
        return _number(value, 0.08, 10.0)
    if name == "ai_action_cooldown_seconds":
        return _number(value, 0.10, 60.0)
    if name == "ai_repeat_cooldown_seconds":
        return _number(value, 0.10, 300.0)
    if name == "ai_max_hamming_distance":
        return _integer(value, 0, 64)
    if name == "max_experience_events":
        return _integer(value, 1000, 1_000_000)
    if name == "max_model_states":
        return _integer(value, 100, 200_000)
    if name == "remote_file_limit_mb":
        return _integer(value, 1, 2048)
    return False


def normalize_config(candidate: object) -> tuple[dict, bool]:
    normalized = default_config()
    if isinstance(candidate, dict):
        for name in normalized:
            if name == "version":
                continue
            value = candidate.get(name)
            if _valid_config_field(name, value):
                normalized[name] = value
    if normalized["ai_repeat_cooldown_seconds"] < normalized["ai_action_cooldown_seconds"]:
        normalized["ai_repeat_cooldown_seconds"] = normalized["ai_action_cooldown_seconds"]
    repaired = candidate != normalized
    return normalized, repaired


def load_or_repair_config() -> tuple[dict, bool]:
    try:
        if CONFIG_PATH.stat().st_size > CONFIG_MAX_BYTES:
            raise ValueError("config too large")
        candidate = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        candidate = None
    config, repaired = normalize_config(candidate)
    if repaired or not CONFIG_PATH.is_file():
        atomic_write_text(CONFIG_PATH, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    return config, repaired


def normalize_relative_path(value: str) -> Path:
    relative = Path(value.replace("\\", "/"))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("非法文件路径")
    destination = (DATA_ROOT / relative).resolve()
    root = DATA_ROOT.resolve()
    if destination == root or root not in destination.parents:
        raise ValueError("文件路径越界")
    if destination.name.casefold() == SCRIPT_NAME.casefold():
        raise ValueError("远程清单不得替换主脚本")
    return destination


def read_https_bytes(url: str, maximum: int, stop_event: threading.Event | None) -> bytes:
    if not url.lower().startswith("https://"):
        raise ValueError("仅允许 HTTPS 下载")
    request = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{CONFIG_VERSION}"})
    chunks: list[bytes] = []
    total = 0
    with urllib.request.urlopen(request, timeout=10, context=ssl.create_default_context()) as response:
        if not response.geturl().lower().startswith("https://"):
            raise ValueError("下载重定向必须保持 HTTPS")
        while True:
            check_cancel(stop_event)
            block = response.read(64 * 1024)
            if not block:
                break
            total += len(block)
            if total > maximum:
                raise ValueError("远程内容超过大小限制")
            chunks.append(block)
    return b"".join(chunks)


def download_verified(
    url: str,
    destination: Path,
    expected_hash: str,
    expected_size: int | None,
    limit: int,
    stop_event: threading.Event | None,
) -> None:
    if not url.lower().startswith("https://"):
        raise ValueError("仅允许 HTTPS 下载")
    request = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{CONFIG_VERSION}"})
    context = ssl.create_default_context()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".download", dir=destination.parent)
    total = 0
    digest = hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as output, urllib.request.urlopen(request, timeout=10, context=context) as response:
            if not response.geturl().lower().startswith("https://"):
                raise ValueError("下载重定向必须保持 HTTPS")
            while True:
                check_cancel(stop_event)
                block = response.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > limit:
                    raise ValueError("远程文件超过大小限制")
                digest.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        check_cancel(stop_event)
        if expected_size is not None and total != expected_size:
            raise ValueError("远程文件大小不符")
        if digest.hexdigest().lower() != expected_hash.lower():
            raise ValueError("远程文件校验失败")
        os.replace(temporary_name, destination)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def apply_remote_manifest(config: dict, report, stop_event: threading.Event | None) -> int:
    manifest_url = os.environ.get("ARTOFWAR3_MANIFEST_URL", "").strip() or config["manifest_url"].strip()
    if not manifest_url:
        return 0
    raw = read_https_bytes(manifest_url, MANIFEST_MAX_BYTES, stop_event)
    manifest = json.loads(raw.decode("utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("远程清单格式错误")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) > 10000:
        raise ValueError("远程清单格式错误")
    changed = 0
    size_limit = int(config["remote_file_limit_mb"]) * 1024 * 1024
    seen: set[Path] = set()
    for index, item in enumerate(files, 1):
        check_cancel(stop_event)
        if not isinstance(item, dict):
            raise ValueError("远程清单文件项格式错误")
        relative = item.get("path")
        url = item.get("url")
        expected_hash = item.get("sha256")
        expected_size = item.get("size")
        if not isinstance(relative, str) or not isinstance(url, str):
            raise ValueError("远程清单缺少路径或 URL")
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash):
            raise ValueError("远程清单缺少有效 SHA-256")
        if expected_size is not None and (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 0
            or expected_size > size_limit
        ):
            raise ValueError("远程清单文件大小无效")
        destination = normalize_relative_path(relative)
        if destination in seen:
            raise ValueError("远程清单包含重复路径")
        seen.add(destination)
        current_ok = destination.is_file()
        if current_ok and expected_size is not None:
            current_ok = destination.stat().st_size == expected_size
        if current_ok:
            current_ok = sha256_file(destination, stop_event).lower() == expected_hash.lower()
        if not current_ok:
            report(f"下载或替换：{relative}", min(90, 20 + int(index * 65 / max(1, len(files)))))
            download_verified(url, destination, expected_hash, expected_size, size_limit, stop_event)
            changed += 1
    return changed


def valid_event(event: object) -> dict | None:
    if not isinstance(event, dict):
        return None
    timestamp = event.get("t")
    state = event.get("s")
    action = event.get("a")
    if not _number(timestamp, 0.0, 10_000_000_000.0):
        return None
    if not isinstance(state, str) or not HEX_STATE_RE.fullmatch(state):
        return None
    if not isinstance(action, str) or not ACTION_RE.fullmatch(action):
        return None
    if action.startswith("M:"):
        _, _, x, y = action.split(":")
        if int(x) > 999 or int(y) > 999:
            return None
    return {"t": timestamp, "s": state, "a": action}


def repair_experience_file(
    limit: int,
    stop_event: threading.Event | None = None,
) -> tuple[int, int, int]:
    EXPERIENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not EXPERIENCE_PATH.exists():
        atomic_write_text(EXPERIENCE_PATH, "")
        return 0, 0, 0
    valid_lines: deque[str] = deque(maxlen=limit)
    damaged = 0
    trimmed = 0
    changed = False
    with EXPERIENCE_PATH.open("r", encoding="utf-8", errors="replace") as stream:
        for index, raw in enumerate(stream, 1):
            if index % 256 == 0:
                check_cancel(stop_event)
            try:
                if len(raw) > EXPERIENCE_LINE_MAX_CHARS:
                    raise ValueError
                event = valid_event(json.loads(raw))
                if event is None:
                    raise ValueError
                canonical = json.dumps(event, separators=(",", ":"))
                if len(valid_lines) == limit:
                    trimmed += 1
                valid_lines.append(canonical)
                if raw != canonical + "\n":
                    changed = True
            except Exception:
                damaged += 1
                changed = True
    check_cancel(stop_event)
    if trimmed:
        changed = True
    if changed:
        atomic_write_lines(EXPERIENCE_PATH, valid_lines, stop_event)
    return len(valid_lines), damaged, trimmed


def read_model_file() -> dict | None:
    if not MODEL_PATH.is_file():
        return None
    try:
        if MODEL_PATH.stat().st_size > MODEL_MAX_COMPRESSED_BYTES:
            return None
        with gzip.open(MODEL_PATH, "rb") as stream:
            raw = stream.read(MODEL_MAX_UNCOMPRESSED_BYTES + 1)
        if len(raw) > MODEL_MAX_UNCOMPRESSED_BYTES:
            return None
        model = json.loads(raw.decode("utf-8"))
        return model if isinstance(model, dict) else None
    except Exception:
        return None


def valid_model(model: object) -> bool:
    if not isinstance(model, dict):
        return False
    if model.get("version") != MODEL_VERSION or model.get("state_bits") != STATE_BITS:
        return False
    if (
        not isinstance(model.get("experience_events"), int)
        or isinstance(model["experience_events"], bool)
        or model["experience_events"] < 0
    ):
        return False
    if not isinstance(model.get("created_utc"), str):
        return False
    states = model.get("states")
    if not isinstance(states, list) or len(states) > 200_000:
        return False
    seen: set[str] = set()
    for item in states:
        if not isinstance(item, list) or len(item) != 4:
            return False
        state, action, count, total = item
        if not isinstance(state, str) or not HEX_STATE_RE.fullmatch(state) or state in seen:
            return False
        seen.add(state)
        if not isinstance(action, str) or not ACTION_RE.fullmatch(action):
            return False
        if action.startswith("M:"):
            _, _, x, y = action.split(":")
            if int(x) > 999 or int(y) > 999:
                return False
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or not isinstance(total, int)
            or isinstance(total, bool)
            or count < 1
            or total < count
        ):
            return False
    return True


def validate_model_file() -> bool:
    return valid_model(read_model_file())


def build_model(
    events: list[dict],
    max_states: int,
    stop_event: threading.Event | None = None,
) -> dict:
    action_counts: dict[str, Counter] = defaultdict(Counter)
    for index, event in enumerate(events, 1):
        if index % 1024 == 0:
            check_cancel(stop_event)
        action_counts[event["s"]][event["a"]] += 1
    ranked = []
    for index, (state, counts) in enumerate(action_counts.items(), 1):
        if index % 1024 == 0:
            check_cancel(stop_event)
        action, count = counts.most_common(1)[0]
        total = sum(counts.values())
        ranked.append((total, count, state, action))
    check_cancel(stop_event)
    ranked.sort(reverse=True)
    states = [[state, action, count, total] for total, count, state, action in ranked[:max_states]]
    return {
        "version": MODEL_VERSION,
        "created_utc": utc_now(),
        "state_bits": STATE_BITS,
        "experience_events": len(events),
        "states": states,
    }


def write_model(model: dict) -> None:
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(model, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    atomic_write_bytes(MODEL_PATH, gzip.compress(raw, compresslevel=9))


def write_integrity_state(stop_event: threading.Event | None = None) -> None:
    files = {}
    for path in (SCRIPT_COPY_PATH, CONFIG_PATH, EXPERIENCE_PATH, MODEL_PATH):
        check_cancel(stop_event)
        if path.is_file():
            files[str(path.relative_to(DATA_ROOT)).replace("\\", "/")] = {
                "size": path.stat().st_size,
                "sha256": sha256_file(path, stop_event),
            }
    state = {"version": 1, "updated_utc": utc_now(), "files": files}
    atomic_write_text(INTEGRITY_PATH, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


if os.name == "nt":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    SW_RESTORE = 9
    SRCCOPY = 0x00CC0020
    HALFTONE = 4
    DIB_RGB_COLORS = 0
    BI_RGB = 0
    VK_ESCAPE = 0x1B
    VK_LBUTTON = 0x01
    VK_RBUTTON = 0x02
    VK_MBUTTON = 0x04
    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010
    MOUSEEVENTF_MIDDLEDOWN = 0x0020
    MOUSEEVENTF_MIDDLEUP = 0x0040

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class INPUTUNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", INPUTUNION)]

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = wintypes.SHORT
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
    gdi32.SetStretchBltMode.restype = ctypes.c_int
    gdi32.StretchBlt.argtypes = [
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
    ]
    gdi32.StretchBlt.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
        wintypes.LPVOID, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL


def window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def enumerate_windows() -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []

    @WNDENUMPROC
    def callback(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            title = window_title(hwnd).strip()
            if title:
                result.append((int(hwnd), title))
        return True

    user32.EnumWindows(callback, 0)
    return result


def find_game_window(keywords: list[str]) -> int | None:
    lowered = [item.casefold() for item in keywords]
    candidates = []
    for hwnd, title in enumerate_windows():
        folded = title.casefold()
        if APP_TITLE.casefold() in folded:
            continue
        score = max((len(keyword) for keyword in lowered if keyword in folded), default=0)
        if score:
            rect = wintypes.RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
                candidates.append((score, area, hwnd))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][2]


def bring_to_front(hwnd: int) -> None:
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)


def game_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width < 100 or height < 100:
        return None
    return rect.left, rect.top, width, height


def capture_state(hwnd: int) -> int | None:
    rect = game_rect(hwnd)
    if rect is None:
        return None
    left, top, width, height = rect
    sample_width = 32
    sample_height = 32
    screen_dc = user32.GetDC(0)
    memory_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, sample_width, sample_height)
    old_object = gdi32.SelectObject(memory_dc, bitmap)
    try:
        gdi32.SetStretchBltMode(memory_dc, HALFTONE)
        ok = gdi32.StretchBlt(
            memory_dc,
            0,
            0,
            sample_width,
            sample_height,
            screen_dc,
            left,
            top,
            width,
            height,
            SRCCOPY,
        )
        if not ok:
            return None
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = sample_width
        info.bmiHeader.biHeight = -sample_height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB
        buffer = (ctypes.c_ubyte * (sample_width * sample_height * 4))()
        rows = gdi32.GetDIBits(memory_dc, bitmap, 0, sample_height, buffer, ctypes.byref(info), DIB_RGB_COLORS)
        if rows != sample_height:
            return None
        values = []
        for cell_y in range(8):
            for cell_x in range(8):
                total = 0
                for y in range(cell_y * 4, cell_y * 4 + 4):
                    row = y * sample_width * 4
                    for x in range(cell_x * 4, cell_x * 4 + 4):
                        index = row + x * 4
                        blue = buffer[index]
                        green = buffer[index + 1]
                        red = buffer[index + 2]
                        total += (red * 77 + green * 150 + blue * 29) >> 8
                values.append(total // 16)
        average = sum(values) // len(values)
        state = 0
        for value in values:
            state = (state << 1) | int(value >= average)
        return state
    finally:
        gdi32.SelectObject(memory_dc, old_object)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(0, screen_dc)


def key_is_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def press_key(vk: int) -> None:
    down = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, 0, 0, 0, 0))
    up = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, 0))
    sequence = (INPUT * 2)(down, up)
    user32.SendInput(2, sequence, ctypes.sizeof(INPUT))


def click_at(hwnd: int, button: str, normalized_x: int, normalized_y: int) -> None:
    rect = game_rect(hwnd)
    if rect is None:
        return
    left, top, width, height = rect
    x = left + max(0, min(999, normalized_x)) * max(1, width - 1) // 999
    y = top + max(0, min(999, normalized_y)) * max(1, height - 1) // 999
    user32.SetCursorPos(x, y)
    flags = {
        "L": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "R": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "M": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }[button]
    user32.mouse_event(flags[0], 0, 0, 0, 0)
    time.sleep(0.025)
    user32.mouse_event(flags[1], 0, 0, 0, 0)


def execute_action(hwnd: int, action: str) -> bool:
    if not user32.IsWindow(hwnd) or int(user32.GetForegroundWindow()) != hwnd:
        return False
    parts = action.split(":")
    if parts[0] == "K":
        press_key(int(parts[1], 16))
    else:
        click_at(hwnd, parts[1], int(parts[2]), int(parts[3]))
    return True


def launch_candidates(config: dict) -> bool:
    command = config.get("game_command") or []
    if command:
        try:
            subprocess.Popen(command, cwd=DATA_ROOT, close_fds=True)
            return True
        except OSError as error:
            log(f"game command failed: {error}")
    keywords = ("art of war 3", "artofwar3", "战争艺术3", "全球冲突")
    locations = [
        DESKTOP,
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ]
    candidates: list[Path] = []
    for location in locations:
        if not location.is_dir():
            continue
        try:
            for path in location.rglob("*.lnk"):
                name = path.stem.casefold()
                if any(keyword in name for keyword in keywords):
                    candidates.append(path)
        except OSError:
            continue
    for path in candidates:
        try:
            os.startfile(path)
            return True
        except OSError:
            continue
    return False


def wait_for_game_window(config: dict, stop_event: threading.Event, report) -> int | None:
    keywords = config["window_title_keywords"]
    hwnd = find_game_window(keywords)
    if hwnd:
        return hwnd
    launched = launch_candidates(config)
    report("正在查找游戏窗口……" if launched else "未发现游戏快捷方式；请先安装并启动游戏。", 12)
    deadline = time.monotonic() + 30
    while not stop_event.is_set() and time.monotonic() < deadline:
        hwnd = find_game_window(keywords)
        if hwnd:
            return hwnd
        time.sleep(0.35)
    return None


TRACKED_KEYS = tuple(
    list(range(0x30, 0x3A))
    + list(range(0x41, 0x5B))
    + [0x08, 0x09, 0x0D, 0x10, 0x11, 0x12, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E]
    + list(range(0x70, 0x7D))
)
MOUSE_KEYS = ((VK_LBUTTON, "L"), (VK_RBUTTON, "R"), (VK_MBUTTON, "M")) if os.name == "nt" else ()


def escape_requested(previous: bool) -> tuple[bool, bool]:
    current = key_is_down(VK_ESCAPE)
    return current and not previous, current


def record_human_experience(hwnd: int, config: dict, stop_event: threading.Event, report) -> int:
    capture_interval = max(0.08, float(config["capture_interval_seconds"]))
    last_capture = 0.0
    current_state: int | None = None
    key_states = {vk: key_is_down(vk) for vk in TRACKED_KEYS}
    mouse_states = {vk: key_is_down(vk) for vk, _ in MOUSE_KEYS}
    last_escape = key_is_down(VK_ESCAPE)
    recorded = 0
    last_window_check = 0.0
    report("人模式：正在记录画面特征和操作；按 ESC 退出。", 20)
    EXPERIENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with EXPERIENCE_PATH.open("a", encoding="utf-8", buffering=1) as stream:
        while not stop_event.is_set():
            triggered, last_escape = escape_requested(last_escape)
            if triggered:
                stop_event.set()
                break
            now = time.monotonic()
            if now - last_window_check >= 1.0:
                last_window_check = now
                if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
                    replacement = find_game_window(config["window_title_keywords"])
                    if replacement:
                        hwnd = replacement
                    else:
                        report("等待游戏窗口恢复……", 20)
                        time.sleep(0.25)
                        continue
            if now - last_capture >= capture_interval:
                current_state = capture_state(hwnd)
                last_capture = now
            foreground = int(user32.GetForegroundWindow()) == hwnd
            if foreground and current_state is not None:
                state_text = f"{current_state:016x}"
                timestamp = time.time()
                for vk in TRACKED_KEYS:
                    down = key_is_down(vk)
                    if down and not key_states[vk]:
                        event = {"t": timestamp, "s": state_text, "a": f"K:{vk:02X}"}
                        stream.write(json.dumps(event, separators=(",", ":")) + "\n")
                        recorded += 1
                    key_states[vk] = down
                rect = game_rect(hwnd)
                if rect:
                    left, top, width, height = rect
                    point = wintypes.POINT()
                    user32.GetCursorPos(ctypes.byref(point))
                    nx = max(0, min(999, (point.x - left) * 999 // max(1, width - 1)))
                    ny = max(0, min(999, (point.y - top) * 999 // max(1, height - 1)))
                    for vk, button in MOUSE_KEYS:
                        down = key_is_down(vk)
                        if down and not mouse_states[vk] and left <= point.x < left + width and top <= point.y < top + height:
                            event = {"t": timestamp, "s": state_text, "a": f"M:{button}:{nx}:{ny}"}
                            stream.write(json.dumps(event, separators=(",", ":")) + "\n")
                            recorded += 1
                        mouse_states[vk] = down
                if recorded and recorded % 20 == 0:
                    report(f"人模式：已记录 {recorded} 个操作；按 ESC 退出。", 20)
            time.sleep(0.018)
    return recorded


def load_experience(limit: int, stop_event: threading.Event | None = None) -> list[dict]:
    events: deque[dict] = deque(maxlen=limit)
    if not EXPERIENCE_PATH.exists():
        return []
    with EXPERIENCE_PATH.open("r", encoding="utf-8", errors="replace") as stream:
        for index, raw in enumerate(stream, 1):
            if index % 256 == 0:
                check_cancel(stop_event)
            try:
                event = valid_event(json.loads(raw))
                if event is not None:
                    events.append(event)
            except Exception:
                continue
    check_cancel(stop_event)
    return list(events)


def upgrade_experience_and_model(
    config: dict,
    report,
    stop_event: threading.Event | None,
) -> tuple[int, int]:
    report("正在校验并压缩经验池……", 15)
    max_events = int(config["max_experience_events"])
    events = load_experience(max_events, stop_event)
    check_cancel(stop_event)
    canonical_lines = (json.dumps(event, separators=(",", ":")) for event in events)
    atomic_write_lines(EXPERIENCE_PATH, canonical_lines, stop_event)
    report(f"经验池包含 {len(events)} 个有效操作，正在生成模型……", 45)
    check_cancel(stop_event)
    model = build_model(events, int(config["max_model_states"]), stop_event)
    check_cancel(stop_event)
    write_model(model)
    write_integrity_state(stop_event)
    state_count = len(model["states"])
    report(f"升级完成：经验 {len(events)} 条，模型状态 {state_count} 个。", 100)
    return len(events), state_count


class PolicyIndex:
    def __init__(self, model: dict):
        self.entries: list[tuple[int, str, int, int]] = []
        self.buckets: dict[int, list[tuple[int, str, int, int]]] = defaultdict(list)
        for state_text, action, count, total in model.get("states", []):
            entry = (int(state_text, 16), action, count, total)
            self.entries.append(entry)
            self.buckets[entry[0] >> 52].append(entry)
        self.fallback = self.entries[:1024]

    def choose(self, state: int, max_distance: int) -> tuple[str, int] | None:
        prefix = state >> 52
        candidates = list(self.buckets.get(prefix, ()))
        if len(candidates) < 8:
            for bit in range(12):
                candidates.extend(self.buckets.get(prefix ^ (1 << bit), ()))
        if not candidates:
            candidates = self.fallback
        best = None
        best_key = None
        for candidate_state, action, count, total in candidates:
            distance = (state ^ candidate_state).bit_count()
            confidence = count / total
            key = (distance, -confidence, -count)
            if best_key is None or key < best_key:
                best_key = key
                best = (action, distance)
        if best is None or best[1] > max_distance:
            best = None
            best_key = None
            for candidate_state, action, count, total in self.entries:
                distance = (state ^ candidate_state).bit_count()
                confidence = count / total
                key = (distance, -confidence, -count)
                if best_key is None or key < best_key:
                    best_key = key
                    best = (action, distance)
            if best is None or best[1] > max_distance:
                return None
        return best


def load_policy() -> PolicyIndex | None:
    model = read_model_file()
    if not valid_model(model) or not model["states"]:
        return None
    return PolicyIndex(model)


def run_ai(hwnd: int, config: dict, policy: PolicyIndex, stop_event: threading.Event, report) -> int:
    interval = max(0.08, float(config["capture_interval_seconds"]))
    action_cooldown = max(0.10, float(config["ai_action_cooldown_seconds"]))
    repeat_cooldown = max(action_cooldown, float(config["ai_repeat_cooldown_seconds"]))
    max_distance = max(0, min(64, int(config["ai_max_hamming_distance"])))
    last_action_time = 0.0
    last_pair: tuple[int, str] | None = None
    last_pair_time = 0.0
    last_escape = key_is_down(VK_ESCAPE)
    actions = 0
    report("AI 模式：正在根据本地模型操作；按 ESC 退出。", 20)
    while not stop_event.is_set():
        triggered, last_escape = escape_requested(last_escape)
        if triggered:
            stop_event.set()
            break
        current = find_game_window(config["window_title_keywords"])
        if current:
            hwnd = current
        else:
            report("AI 模式：等待游戏窗口恢复……", 20)
            time.sleep(0.35)
            continue
        if int(user32.GetForegroundWindow()) != hwnd:
            bring_to_front(hwnd)
            time.sleep(0.15)
        if int(user32.GetForegroundWindow()) != hwnd:
            report("AI 模式：等待游戏窗口获得焦点……", 20)
            time.sleep(0.20)
            continue
        state = capture_state(hwnd)
        now = time.monotonic()
        if state is not None and now - last_action_time >= action_cooldown:
            choice = policy.choose(state, max_distance)
            if choice:
                action, distance = choice
                pair = (state, action)
                repeated_too_soon = pair == last_pair and now - last_pair_time < repeat_cooldown
                if not repeated_too_soon:
                    if not execute_action(hwnd, action):
                        time.sleep(0.10)
                        continue
                    actions += 1
                    last_action_time = now
                    last_pair = pair
                    last_pair_time = now
                    if actions % 10 == 0:
                        report(f"AI 模式：已执行 {actions} 次操作，最近匹配距离 {distance}；按 ESC 退出。", 20)
        time.sleep(interval)
    return actions


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.mode = "空闲"
        self.closing = False
        self.status = tk.StringVar(value="就绪。请选择模式。")
        self.mode_text = tk.StringVar(value="当前模式：空闲")
        self.progress = tk.DoubleVar(value=0)
        self.buttons: list[tk.Button] = []
        self.last_escape_state = False
        self._build()
        self.root.after(50, self._poll_global_escape)
        self.root.after(80, self._drain_events)

    def _build(self) -> None:
        self.root.title(APP_TITLE)
        self.root.geometry("620x520")
        self.root.minsize(540, 460)
        self.root.configure(bg="#111318")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Escape>", lambda _: self.request_stop())

        title = tk.Label(
            self.root,
            text="ART OF WAR 3",
            font=("Segoe UI", 26, "bold"),
            fg="#f3f5f7",
            bg="#111318",
        )
        title.pack(pady=(28, 4))
        subtitle = tk.Label(
            self.root,
            text="文件管理 · 人类经验采集 · 本地模型升级 · AI 操作",
            font=("Microsoft YaHei UI", 10),
            fg="#9ca5b4",
            bg="#111318",
        )
        subtitle.pack(pady=(0, 24))

        grid = tk.Frame(self.root, bg="#111318")
        grid.pack(fill="both", expand=True, padx=46)
        definitions = [
            ("文件", self.start_file_mode),
            ("人", self.start_human_mode),
            ("升级", self.start_upgrade_mode),
            ("AI", self.start_ai_mode),
        ]
        for index, (label, command) in enumerate(definitions):
            button = tk.Button(
                grid,
                text=label,
                command=command,
                font=("Microsoft YaHei UI", 20, "bold"),
                fg="#f7f8fa",
                bg="#242a33",
                activeforeground="#ffffff",
                activebackground="#343d49",
                relief="flat",
                bd=0,
                cursor="hand2",
            )
            button.grid(row=index // 2, column=index % 2, sticky="nsew", padx=8, pady=8)
            self.buttons.append(button)
        grid.rowconfigure(0, weight=1)
        grid.rowconfigure(1, weight=1)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        footer = tk.Frame(self.root, bg="#111318")
        footer.pack(fill="x", padx=46, pady=(14, 26))
        tk.Label(
            footer,
            textvariable=self.mode_text,
            anchor="w",
            font=("Microsoft YaHei UI", 10, "bold"),
            fg="#dce2ea",
            bg="#111318",
        ).pack(fill="x")
        tk.Label(
            footer,
            textvariable=self.status,
            anchor="w",
            justify="left",
            wraplength=520,
            font=("Microsoft YaHei UI", 9),
            fg="#aeb7c4",
            bg="#111318",
        ).pack(fill="x", pady=(5, 8))
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("AoW.Horizontal.TProgressbar", troughcolor="#252a32", background="#d7dce3", borderwidth=0)
        ttk.Progressbar(
            footer,
            variable=self.progress,
            maximum=100,
            style="AoW.Horizontal.TProgressbar",
        ).pack(fill="x")
        tk.Label(
            footer,
            text="模式运行时按 ESC 返回",
            anchor="e",
            font=("Microsoft YaHei UI", 8),
            fg="#717b89",
            bg="#111318",
        ).pack(fill="x", pady=(7, 0))

    def post(self, kind: str, *payload) -> None:
        self.events.put((kind, payload))

    def report(self, text: str, progress: int | float | None = None) -> None:
        self.post("status", text, progress)

    def set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for button in self.buttons:
            button.configure(state=state)

    def start_mode(self, name: str, target, minimize: bool = False) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.mode = name
        self.mode_text.set(f"当前模式：{name}")
        self.status.set("正在启动……")
        self.progress.set(2)
        self.stop_event = threading.Event()
        self.last_escape_state = key_is_down(VK_ESCAPE)
        self.set_busy(True)
        if minimize:
            self.root.after(250, self.root.iconify)
        self.worker = threading.Thread(target=self._worker_wrapper, args=(target,), daemon=True)
        self.worker.start()

    def _worker_wrapper(self, target) -> None:
        try:
            target()
        except ModeCancelled:
            self.post("cancelled", f"已退出{self.mode}模式。")
        except Exception as error:
            log(f"{self.mode} mode error: {error!r}")
            self.post("error", f"{self.mode}模式失败：{error}")
        finally:
            self.post("done")

    def start_file_mode(self) -> None:
        self.start_mode("文件", self.file_mode)

    def start_human_mode(self) -> None:
        self.start_mode("人", self.human_mode, minimize=True)

    def start_upgrade_mode(self) -> None:
        self.start_mode("升级", self.upgrade_mode)

    def start_ai_mode(self) -> None:
        self.start_mode("AI", self.ai_mode, minimize=True)

    def file_mode(self) -> None:
        self.report("正在创建并检查桌面 ArtOfWar3 文件夹……", 8)
        check_cancel(self.stop_event)
        script_synced = ensure_layout()
        if not script_synced:
            raise OSError("无法将主脚本同步到桌面 ArtOfWar3 文件夹")
        config, repaired_config = load_or_repair_config()
        check_cancel(self.stop_event)
        self.report("正在检查经验池完整性……", 28)
        valid_events, damaged_events, trimmed_events = repair_experience_file(
            int(config["max_experience_events"]), self.stop_event
        )
        self.report("正在检查可选远程文件清单……", 45)
        downloaded = apply_remote_manifest(config, self.report, self.stop_event)
        check_cancel(self.stop_event)
        if downloaded:
            config, repaired_after_download = load_or_repair_config()
            repaired_config = repaired_config or repaired_after_download
            valid_events, damaged_after_download, trimmed_after_download = repair_experience_file(
                int(config["max_experience_events"]), self.stop_event
            )
            damaged_events += damaged_after_download
            trimmed_events += trimmed_after_download
        model_rebuilt = False
        if not validate_model_file():
            self.report("模型缺失或损坏，正在从有效经验重建……", 82)
            events = load_experience(int(config["max_experience_events"]), self.stop_event)
            write_model(build_model(events, int(config["max_model_states"]), self.stop_event))
            model_rebuilt = True
        write_integrity_state(self.stop_event)
        messages = [f"有效经验 {valid_events} 条", "主脚本完整"]
        if repaired_config:
            messages.append("配置已补全或修复")
        if damaged_events:
            messages.append(f"移除损坏经验 {damaged_events} 条")
        if trimmed_events:
            messages.append(f"裁剪过量经验 {trimmed_events} 条")
        if downloaded:
            messages.append(f"下载或替换 {downloaded} 个文件")
        elif not config["manifest_url"].strip() and not os.environ.get("ARTOFWAR3_MANIFEST_URL", "").strip():
            messages.append("核心文件已本地自举，无需下载")
        if model_rebuilt:
            messages.append("模型已补全或修复")
        else:
            messages.append("模型完整")
        self.report("文件模式完成：" + "；".join(messages) + "。", 100)

    def human_mode(self) -> None:
        if not ensure_layout():
            raise OSError("无法准备桌面 ArtOfWar3 文件夹")
        config, _ = load_or_repair_config()
        hwnd = wait_for_game_window(config, self.stop_event, self.report)
        if hwnd is None:
            if self.stop_event.is_set():
                self.report("已退出人模式。", 100)
            else:
                self.report("未找到游戏窗口。可先启动游戏，或在配置中设置游戏启动命令。", 100)
            return
        bring_to_front(hwnd)
        recorded = record_human_experience(hwnd, config, self.stop_event, self.report)
        self.report(f"已退出人模式，本次记录 {recorded} 个操作。", 100)

    def upgrade_mode(self) -> None:
        if not ensure_layout():
            raise OSError("无法准备桌面 ArtOfWar3 文件夹")
        config, _ = load_or_repair_config()
        repair_experience_file(int(config["max_experience_events"]), self.stop_event)
        events, states = upgrade_experience_and_model(config, self.report, self.stop_event)
        if events == 0:
            self.report("升级完成，但经验池为空。先在人模式中实际操作游戏，再次升级。", 100)
        elif states == 0:
            self.report("经验池已整理，但没有可用于模型的状态。", 100)

    def ai_mode(self) -> None:
        if not ensure_layout():
            raise OSError("无法准备桌面 ArtOfWar3 文件夹")
        config, _ = load_or_repair_config()
        policy = load_policy()
        if policy is None:
            self.report("没有可用模型。请先用“人”模式采集经验，再点击“升级”。", 100)
            return
        hwnd = wait_for_game_window(config, self.stop_event, self.report)
        if hwnd is None:
            if self.stop_event.is_set():
                self.report("已退出 AI 模式。", 100)
            else:
                self.report("未找到游戏窗口，AI 模式已退出。", 100)
            return
        bring_to_front(hwnd)
        actions = run_ai(hwnd, config, policy, self.stop_event, self.report)
        self.report(f"已退出 AI 模式，本次执行 {actions} 次操作。", 100)

    def _poll_global_escape(self) -> None:
        if self.closing:
            return
        current = key_is_down(VK_ESCAPE)
        if self.worker and self.worker.is_alive() and current and not self.last_escape_state:
            self.request_stop()
        self.last_escape_state = current
        self.root.after(50, self._poll_global_escape)

    def request_stop(self) -> None:
        if self.worker and self.worker.is_alive():
            self.status.set(f"正在退出{self.mode}模式……")
            self.stop_event.set()

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "status":
                    text, progress = payload
                    self.status.set(text)
                    if progress is not None:
                        self.progress.set(progress)
                elif kind in {"error", "cancelled"}:
                    self.status.set(payload[0])
                    self.progress.set(100)
                elif kind == "done":
                    self.mode = "空闲"
                    self.mode_text.set("当前模式：空闲")
                    self.set_busy(False)
                    self.root.deiconify()
                    self.root.lift()
                    self.root.focus_force()
        except queue.Empty:
            pass
        if not self.closing:
            self.root.after(80, self._drain_events)

    def close(self) -> None:
        self.closing = True
        self.stop_event.set()
        self.root.destroy()


def runtime_error() -> str | None:
    if os.name != "nt":
        return "本程序仅支持 Windows 11 x64。"
    windows = sys.getwindowsversion()
    if windows.major < 10 or windows.build < WINDOWS_11_MIN_BUILD:
        return "本程序仅支持 Windows 11 x64（系统内部版本 22000 或更高）。"
    if sys.maxsize <= 2**32 or platform.machine().lower() not in {"amd64", "x86_64"}:
        return "本程序需要 Windows 11 x64 与 64 位 Python。"
    if sys.version_info[:3] != REQUIRED_PYTHON:
        required = ".".join(map(str, REQUIRED_PYTHON))
        current = ".".join(map(str, sys.version_info[:3]))
        return f"本程序需要 Python {required}，当前为 Python {current}。"
    if tk is None or ttk is None:
        return "当前 Python 缺少 tkinter/Tcl-Tk 组件，请使用包含 Tcl/Tk 的 Python 3.12.10。"
    return None


def show_error(message: str) -> None:
    if tk is None:
        print(message, file=sys.stderr)
        return
    try:
        root = tk.Tk()
        root.withdraw()
        from tkinter import messagebox

        messagebox.showerror(APP_TITLE, message)
        root.destroy()
    except Exception:
        print(message, file=sys.stderr)


def main() -> int:
    problem = runtime_error()
    if problem:
        show_error(problem)
        return 1
    if not ensure_layout():
        show_error("无法创建或写入桌面 ArtOfWar3 文件夹。")
        return 1
    log("application started")
    root = tk.Tk()
    App(root)
    root.mainloop()
    log("application stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
