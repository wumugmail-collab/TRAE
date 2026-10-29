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

try:
    import pyperclip
except Exception:
    pyperclip = None  # 没有剪贴板库时只打印/通知，不影响监测

# 兼容两代 RapidOCR 包名：老包 rapidocr-onnxruntime / 新包 rapidocr
try:
    from rapidocr_onnxruntime import RapidOCR
except Exception:
    try:
        from rapidocr import RapidOCR
    except Exception:
        RapidOCR = None

try:
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
        if pyperclip is not None:
            pyperclip.copy(message)
            print("（口令已复制到剪贴板，可直接去支付宝粘贴）", flush=True)
    except Exception:
        pass


# 支付宝口令红包口令特征（按实际观察）：
#   - 长度 >5，常见 6～20，最长可达十几个字符
#   - 主体是「中文 + 数字」，少量含英文字母
#   - 极少带符号（符号占比低）
_MIN_LEN = 6
_MAX_LEN = 20
# 明显是引导语/标签的词，避免被当作口令
_LABEL_WORDS = ("支付宝", "红包", "口令", "输入", "扫一扫", "领取", "点击", "复制", "回复", "打开")
# 明显不是口令的界面噪声（有效期、余额、时间等），避免兜底时误判
_NOISE_WORDS = ("有效期", "有效", "失效", "剩余", "余额", "存入", "小时", "分钟", "秒钟",
                "截止", "截至", "日期", "时间", "金额", "成功", "失败", "已领")
# 候选片段：两端为中文/字母/数字，中间允许少量常见符号
_CANDIDATE_RE = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9]"
    r"[\u4e00-\u9fffA-Za-z0-9\-·]{4,18}"
    r"[\u4e00-\u9fffA-Za-z0-9]"
)


def _clean(s):
    return s.strip(" \t\n\r，。！？、；,.!?;:：\"“”'‘’·~+-（）()【】[]<>/\\")


def _looks_like_kouling(s):
    """校验是否符合口令特征：长度 6~20，中英数占比 >= 70%，且不含界面噪声词。"""
    s = _clean(s)
    if not (_MIN_LEN <= len(s) <= _MAX_LEN):
        return False
    if any(w in s for w in _LABEL_WORDS):
        return False
    if any(w in s for w in _NOISE_WORDS):
        return False
    core = sum(
        1 for c in s
        if ("\u4e00" <= c <= "\u9fff") or ("0" <= c <= "9") or ("a" <= c.lower() <= "z")
    )
    return core / len(s) >= 0.7


# 口令前的提示词：口令 / 口令是 / 复制 / 请复制 ……
_MARK_RE = re.compile(r"(?:请)?(?:红包)?(?:口令|复制)(?:就是|叫做|是|为|叫|请复制|:|：|\s)*")
# 口令后常见的引导语，遇到就截断（OCR 常把空格吃掉，导致「复制7xK9mP2打开支付宝」连成一片）
_MARK_TAIL_CUT_RE = re.compile(
    r"\n|[\u3000，。！？、；,.;:：]"
    r"|打开支付宝|打开|支付宝|领取|点击|输入|复制|口令|有效期|请尽快|赶快|立即|链接|红包"
)
_SEP_RE = re.compile(r"[ \t，。！？、；,.!?;:：\"“”'‘’（）()【】\[\]]")


def extract_kouling(text):
    """从 OCR 文本里抠出口令短语。找不到时返回 None。"""
    if not text:
        return None

    # 1) 优先：紧跟在「口令 / 复制」等提示词之后，遇到引导语即截断
    for m in _MARK_RE.finditer(text):
        tail = text[m.end():m.end() + 30]
        tail = _MARK_TAIL_CUT_RE.split(tail)[0]
        tail = _SEP_RE.sub("", tail)
        if tail and _looks_like_kouling(tail):
            return tail

    # 2) 兜底：逐行找符合特征的短语（口令常独占一行/一个控件）
    for line in text.splitlines():
        for c in _CANDIDATE_RE.findall(line):
            if _looks_like_kouling(c):
                return _clean(c)

    return None


def ocr_image(img_bgr):
    """对一张图像做 OCR，返回拼接文本。兼容两代 RapidOCR 返回格式。"""
    out = _OCR(img_bgr)
    if isinstance(out, tuple):          # 老版：(result, elapse)
        result = out[0]
    else:                               # 新版：RapidOCROutput 或 None
        result = out
    if result is None:
        return ""
    # 新版对象带 txts 属性
    if hasattr(result, "txts"):
        return "\n".join(t for t in result.txts if t)
    # 老版：list of [box, text, score]
    lines = []
    for item in result:
        if not item:
            continue
        txt = item[1] if len(item) > 1 else str(item)
        if txt:
            lines.append(str(txt))
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
        last_k = None  # 同一个口令只通知一次，避免画面抖动时反复弹窗
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
            if k and k != last_k:
                notify("发现口令红包", k)
                last_k = k
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