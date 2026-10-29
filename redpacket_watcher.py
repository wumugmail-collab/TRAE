#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
支付宝口令红包 · 屏幕监测 + 本地 OCR 提取工具

原理：
  1. 周期性截取屏幕（默认全屏，可指定区域）
  2. 与上一帧做像素差异比较，画面变了才触发 OCR，省资源
  3. 本地 OCR 识别中文（RapidOCR / onnxruntime，无需联网、不传云端）
  4. 用规则/正则提取"口令"短语
  5. 桌面通知 + 自动复制口令到剪贴板 + 控制台打印

安装依赖（在你自己的电脑上执行，Windows 用 cmd/PowerShell，Mac/Linux 用终端）：
  pip install -U "rapidocr-onnxruntime" mss pillow numpy pyperclip plyer

  注意：首次运行会联网下载 OCR 模型；Linux 下剪贴板需要系统装有 xclip/xsel。
  Mac 上如需截屏权限，请在「系统设置 → 隐私与安全 → 屏幕录制」里授权你的终端/Python。

用法：
  python redpacket_watcher.py                      # 监测整个主屏
  python redpacket_watcher.py --region 0 0 1280 720  # 只监测指定区域 x y w h
  python redpacket_watcher.py --interval 2         # 每 2 秒检测一次（默认 1.5）
  python redpacket_watcher.py --once images/xx.png # 单张图片调试，不进循环
  按 Ctrl+C 退出。
"""

import argparse
import re
import sys
import time

import numpy as np
from PIL import Image
import pyperclip

try:
    from rapidocr_onnxruntime import RapidOCR

    _OCR = RapidOCR()
    HAS_OCR = True
except Exception as _e:  # 依赖未装或模型加载失败
    _OCR = None
    HAS_OCR = False
    _OCR_ERR = _e

try:
    from plyer import notification

    HAS_NOTIFY = True
except Exception:
    HAS_NOTIFY = False


def notify(title, message):
    """桌面通知 + 控制台打印 + 复制到剪贴板。"""
    print(f"\n[口令红包] {title}: {message}", flush=True)
    if HAS_NOTIFY:
        try:
            notification.notify(title=title, message=message, timeout=10)
        except Exception:
            pass
    try:
        pyperclip.copy(message)
        print("（口令已复制到剪贴板，可直接去支付宝粘贴）", flush=True)
    except Exception:
        pass


# 支付宝口令常见形态：口令后面紧跟一串短语；或引导语里带引号。
_PATTERNS = [
    re.compile(r"口令[：:\s]*['\"]?([^\s，。！？、；,.!?;:]{2,24})"),
    re.compile(r"口令[：:\s]*(.{2,24})"),
    re.compile(r"(?:输入|回复|发送|说)[^\u4e00-\u9fff]*(['\"“]([\u4e00-\u9fffA-Za-z0-9]{2,16})['\"”])"),
]


def extract_kouling(text):
    """从 OCR 文本里抠出口令短语。找不到时返回 None。"""
    if not text:
        return None
    # 去掉空白便于正则
    compact = re.sub(r"\s+", "", text)
    for pat in _PATTERNS:
        m = pat.search(compact)
        if m:
            val = m.group(2) if m.lastindex and m.lastindex >= 2 else m.group(1)
            val = val.strip().strip("，。！？、；,.!?;:：\"“”'‘’ ")
            if val and 2 <= len(val) <= 24:
                return val
    return None


def ocr_image(img_bgr):
    """对一张 BGR numpy 图做 OCR，返回拼接文本。"""
    result, _ = _OCR(img_bgr)
    lines = []
    if result:
        for item in result:
            # item: [4个角点, 文字, 置信度]
            txt = item[1]
            if txt:
                lines.append(txt)
    return "\n".join(lines)


def frame_diff(a, b):
    """下采样后比较两帧差异比，返回 float(0~1)。"""
    def small(x):
        h, w = x.shape[:2]
        scale = 64 / max(h, w)
        return x[:: max(1, int(1 / scale)), :: max(1, int(1 / scale))]

    sa, sb = small(a), small(b)
    if sa.shape != sb.shape:
        return 1.0
    return float(np.mean((sa.astype(np.int16) - sb.astype(np.int16)) ** 2)) / (255 ** 2)


def run_once(path):
    if not HAS_OCR:
        print(f"OCR 引擎不可用：{_OCR_ERR}")
        sys.exit(1)
    img = Image.open(path).convert("RGB")
    arr = np.array(img)[:, :, ::-1]  # RGB -> BGR
    text = ocr_image(arr)
    print("=== OCR 原始文本 ===")
    print(text)
    k = extract_kouling(text)
    print("=== 提取结果 ===")
    print(k if k else "（未识别出口令，请尝试调整区域或截图清晰度）")


def monitor(region, interval, threshold):
    import mss

    if not HAS_OCR:
        print(f"OCR 引擎不可用：{_OCR_ERR}")
        print("请先安装：pip install -U rapidocr-onnxruntime")
        sys.exit(1)

    with mss.mss() as sct:
        if region:
            x, y, w, h = region
            mon = {"left": x, "top": y, "width": w, "height": h}
        else:
            mon = sct.monitors[1]  # 主屏

        last = None
        print(f"开始监测区域 {mon}，间隔 {interval}s，Ctrl+C 退出。")
        while True:
            shot = sct.grab(mon)
            frame = np.asarray(shot)[:, :, :3]  # BGRA -> BGR
            if last is not None and frame_diff(frame, last) < threshold:
                time.sleep(interval)
                last = frame
                continue

            # 画面变化，触发 OCR
            text = ocr_image(frame)
            k = extract_kouling(text)
            if k:
                notify("发现口令红包", k)
            last = frame
            time.sleep(interval)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", nargs=4, type=int, metavar=("X", "Y", "W", "H"),
                    help="只监测该矩形区域")
    ap.add_argument("--interval", type=float, default=1.5, help="检测间隔秒")
    ap.add_argument("--threshold", type=float, default=0.002, help="差异阈值，越大越不敏感")
    ap.add_argument("--once", help="对单张图片跑一次 OCR 调试，不进监测循环")
    args = ap.parse_args()

    if args.once:
        run_once(args.once)
        return
    monitor(tuple(args.region) if args.region else None, args.interval, args.threshold)


if __name__ == "__main__":
    main()