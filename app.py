"""口令红包监测 —— 桌面 App（macOS / Windows）。

后台不停截屏，找到支付宝「口令红包」卡片下方那排红底白字口令，
自动复制到剪贴板并响一声提示，界面同步显示同款卡片。

运行：
    python3 app.py

依赖见 requirements.txt。首次运行会加载 OCR 模型，约 1~2 秒。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

import detector

logging.getLogger("RapidOCR").setLevel(logging.WARNING)

HERE = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    "interval": 1.0,      # 扫描间隔（秒）
    "auto_copy": True,    # 识别到新口令自动复制
    "sound": True,        # 提示音
    "on_top": False,      # 窗口置顶
    "monitor": 1,         # 1=主屏，0=全部屏幕
    "region": None,       # [x, y, w, h]，None 表示整个屏幕
    "min_confidence": 0.55,
}


# ---------------------------------------------------------------- 配置

def config_path() -> str:
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/HongbaoWatcher")
    elif sys.platform.startswith("win"):
        base = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "HongbaoWatcher")
    else:
        base = os.path.expanduser("~/.config/HongbaoWatcher")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "config.json")


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(config_path(), encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------- 提示音

def play_sound() -> None:
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform.startswith("win"):
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        else:
            print("\a", flush=True)
    except Exception:
        pass


def copy_to_clipboard(text: str) -> bool:
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception:
        pass
    try:
        if sys.platform == "darwin":
            subprocess.run("pbcopy", input=text.encode(), check=False)
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------- 监测器

class Watcher:
    """后台扫描线程 + 对外可查询的状态。"""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_code = ""
        self._black_frames = 0
        self._state = {
            "running": True,
            "code": "",
            "chars": [],
            "confidence": 0.0,
            "at": "",
            "count": 0,
            "warning": "",
        }
        self._on_hit = None  # 可选回调，UI 就绪后设置

    # --- 生命周期
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="rp-watcher")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # --- 状态
    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def set_running(self, running: bool) -> None:
        with self._lock:
            self._state["running"] = bool(running)

    def reset_detection(self) -> None:
        """清掉「上一个口令」，让同一张卡片也能再次触发。"""
        self._last_code = ""

    # --- 核心：处理一帧
    def handle_image(self, img) -> detector.CodeHit | None:
        # 截到全黑画面通常是 macOS 没给「屏幕录制」权限
        if img is not None and float(img.mean()) < 8:
            self._black_frames += 1
        else:
            self._black_frames = 0
        with self._lock:
            self._state["count"] += 1
            if self._black_frames >= 3:
                self._state["warning"] = ("macOS 需要在「系统设置 → 隐私与安全性 → 屏幕录制」"
                                          "里勾选本应用，并重启 App")
            else:
                self._state["warning"] = ""

        hit = detector.extract_code(img) if img is not None else None
        if not hit or hit.confidence < self.cfg.get("min_confidence", 0.55):
            return None
        if hit.code == self._last_code:
            return None

        self._last_code = hit.code
        now = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._state.update({
                "code": hit.code,
                "chars": hit.chars,
                "confidence": hit.confidence,
                "at": now,
                "rect": list(hit.rect),
            })
        if self.cfg.get("auto_copy", True):
            copy_to_clipboard(hit.code)
        if self.cfg.get("sound", True):
            play_sound()
        if self._on_hit:
            try:
                self._on_hit(hit)
            except Exception:
                pass
        return hit

    def scan_once(self) -> detector.CodeHit | None:
        img = self._grab()
        return self.handle_image(img) if img is not None else None

    def _grab(self):
        region = self.cfg.get("region")
        rect = tuple(region) if region and len(region) == 4 else None
        try:
            return detector.capture_region(rect, self.cfg["monitor"]) if rect \
                else detector.capture(self.cfg["monitor"])
        except Exception:
            return None

    def _loop(self) -> None:
        while not self._stop.is_set():
            interval = max(0.3, float(self.cfg.get("interval", 1.0)))
            if self.snapshot()["running"]:
                img = self._grab()
                if img is not None:
                    self.handle_image(img)
            time.sleep(interval)


# ---------------------------------------------------------------- WebView API

class Api:
    def __init__(self, watcher: Watcher) -> None:
        self.watcher = watcher
        self.window = None

    # 供前端轮询
    def get_state(self) -> dict:
        s = self.watcher.snapshot()
        s["interval"] = self.watcher.cfg.get("interval", 1.0)
        return s

    def get_config(self) -> dict:
        return dict(self.watcher.cfg)

    def save_config(self, cfg: dict) -> dict:
        w = self.watcher
        for key in ("interval", "auto_copy", "sound", "on_top", "monitor", "region"):
            if key in cfg:
                w.cfg[key] = cfg[key]
        save_config(w.cfg)
        w.reset_detection()
        if self.window is not None:
            self.window.on_top = bool(w.cfg.get("on_top"))
        return dict(w.cfg)

    def set_running(self, running: bool) -> bool:
        self.watcher.set_running(running)
        if running:
            self.watcher.reset_detection()
        return running

    def copy(self, text: str) -> bool:
        return copy_to_clipboard(text or "")

    def scan_now(self) -> dict:
        hit = self.watcher.scan_once()
        if not hit:
            return {"ok": False}
        return {"ok": True, "code": hit.code}

    def quit(self) -> None:
        self.watcher.stop()
        if self.window is not None:
            self.window.destroy()


# ---------------------------------------------------------------- 入口

def main() -> None:
    cfg = load_config()
    watcher = Watcher(cfg)
    api = Api(watcher)

    try:
        import webview
    except ImportError:
        raise SystemExit(
            "缺少 pywebview。请先执行：\n"
            "    pip3 install -r requirements.txt\n"
            "（macOS 还需系统自带的 WKWebView，Windows 使用 Edge WebView2）"
        )

    watcher.start()
    window = webview.create_window(
        "口令红包监测",
        os.path.join(HERE, "ui", "index.html"),
        js_api=api,
        width=430,
        height=900,
        resizable=True,
        min_size=(380, 720),
        on_top=bool(cfg.get("on_top")),
        background_color="#f7f2f4",
    )
    api.window = window
    webview.start()
    watcher.stop()


if __name__ == "__main__":
    main()
