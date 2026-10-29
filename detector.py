"""口令红包识别核心：从屏幕截图中定位「口令条」并 OCR 出文字。

口令条 = 支付宝口令红包卡片下方那一排红底白字的方块（例如 小 吴 厉 害 1 2 4 3）。

对外接口：
    capture(monitor=1)          -> BGR ndarray
    capture_region(rect, mon)   -> BGR ndarray
    extract_code(img)           -> CodeHit | None
    scan_screen(monitor, region)-> CodeHit | None

命令行自检：
    python3 detector.py --image shot.png
    python3 detector.py --screen --debug      # 标注图存到 debug/last_scan.png
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import time
from dataclasses import asdict, dataclass

import cv2
import numpy as np
from rapidocr import RapidOCR

logging.getLogger("RapidOCR").setLevel(logging.WARNING)

MIN_BOX_PX = 14      # 单个方块最小边长
MAX_BOX_PX = 420     # 单个方块最大边长
MIN_ROW_BOXES = 4    # 至少这么多个方块才认为是一行口令
ROW_TOLERANCE = 0.75 # 同一行方块中心 y 的允许偏差（相对方块高度）
MAX_WORK_WIDTH = 1920  # 检测前先把超宽截图缩到这个宽度，4K/Retina 也够快
PUNCT = "「」『』“”‘’\"'、,，。.:：;；!！?？()（）[]【】《》 \t\n"

_engine: RapidOCR | None = None


@dataclass
class CodeHit:
    code: str
    chars: list[str]
    confidence: float
    rect: tuple[int, int, int, int]  # 口令条区域 (x, y, w, h)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["rect"] = list(self.rect)
        return d


def _get_engine() -> RapidOCR:
    global _engine
    if _engine is None:
        _engine = RapidOCR(params={"Global.log_level": "error"})
    return _engine


# ---------------------------------------------------------------- 截屏

def capture(monitor: int = 1) -> np.ndarray:
    """截取整个屏幕，返回 BGR 图像。monitor=1 为主屏，0 为全部屏幕。"""
    import mss
    with mss.mss() as sct:
        mon = sct.monitors[monitor] if monitor < len(sct.monitors) else sct.monitors[1]
        shot = np.asarray(sct.grab(mon))
    return cv2.cvtColor(shot, cv2.COLOR_BGRA2BGR)


def capture_region(rect: tuple[int, int, int, int], monitor: int = 1) -> np.ndarray:
    """只截取屏幕上一块区域 (x, y, w, h)，用于缩小监测范围、降低开销。"""
    import mss
    x, y, w, h = rect
    with mss.mss() as sct:
        off = sct.monitors[monitor] if monitor < len(sct.monitors) else sct.monitors[1]
        shot = np.asarray(sct.grab({
            "left": off["left"] + x, "top": off["top"] + y,
            "width": w, "height": h,
        }))
    return cv2.cvtColor(shot, cv2.COLOR_BGRA2BGR)


# ---------------------------------------------------------------- 定位红色方块

def _red_mask(img: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    m = cv2.bitwise_or(
        cv2.inRange(hsv, np.array([0, 70, 60], np.uint8), np.array([13, 255, 255], np.uint8)),
        cv2.inRange(hsv, np.array([162, 70, 60], np.uint8), np.array([180, 255, 255], np.uint8)),
    )
    # 把方块中的白色字填实，避免一个字被切成碎片
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    return m


def _has_white_text(img: np.ndarray, box: tuple[int, int, int, int]) -> bool:
    """口令方块 = 深红底 + 明显更亮的白字；浅色底（卡片背景、粉色文字）直接排除。"""
    x, y, w, h = box
    crop = cv2.cvtColor(img[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY)
    if crop.size == 0:
        return False
    med = float(np.median(crop))
    if med > 150:                        # 底色太亮，不是深红方块
        return False
    bright = float((crop > max(med + 70, 170)).mean())
    return bright > 0.04


def find_boxes(img: np.ndarray) -> list[tuple[int, int, int, int]]:
    """找出所有像「红底白字方块」的候选矩形。"""
    h, w = img.shape[:2]
    mask = _red_mask(img)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes: list[tuple[int, int, int, int]] = []
    for c in contours:
        x, y, bw, bh = cv2.boundingRect(c)
        area = cv2.contourArea(c)
        if bw < MIN_BOX_PX or bh < MIN_BOX_PX or bw > MAX_BOX_PX or bh > MAX_BOX_PX:
            continue
        if area < 0.6 * bw * bh:            # 不够实心（空心框、装饰角标）
            continue
        if not 0.5 <= bw / bh <= 1.9:       # 形状不像单个字符方块
            continue
        if not _has_white_text(img, (x, y, bw, bh)):
            continue
        boxes.append((int(x), int(y), int(bw), int(bh)))
    return boxes


def group_rows(boxes: list[tuple[int, int, int, int]]) -> list[list[tuple[int, int, int, int]]]:
    """把同一水平线上的、尺寸相近的方块归为一行。"""
    rows: list[list[tuple[int, int, int, int]]] = []
    for box in sorted(boxes, key=lambda b: b[1] + b[3] / 2):
        x, y, bw, bh = box
        cy = y + bh / 2
        for row in rows:
            ry = float(np.median([b[1] + b[3] / 2 for b in row]))
            rh = float(np.median([b[3] for b in row]))
            if abs(cy - ry) <= ROW_TOLERANCE * rh and 0.5 <= bh / rh <= 2.0:
                row.append(box)
                break
        else:
            rows.append([box])
    for row in rows:
        row.sort(key=lambda b: b[0])
    return rows


def _split_merged_band(img: np.ndarray) -> list[list[tuple[int, int, int, int]]]:
    """低分辨率下相邻方块可能粘成一条，这里按方块边长切成等宽小格。"""
    h, w = img.shape[:2]
    mask = _red_mask(img)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    rows: list[list[tuple[int, int, int, int]]] = []
    for c in contours:
        x, y, bw, bh = cv2.boundingRect(c)
        if cv2.contourArea(c) < 0.6 * bw * bh or bh < MIN_BOX_PX:
            continue
        if not 1.9 < bw / bh <= 20:
            continue
        cell = bh if bh >= MIN_BOX_PX else bw
        n = max(1, round(bw / cell))
        if n < MIN_ROW_BOXES:
            continue
        step = bw / n
        cells = [(int(x + i * step), int(y), int(step), int(bh)) for i in range(n)]
        if sum(_has_white_text(img, c) for c in cells) >= max(3, int(0.7 * n)):
            rows.append(cells)
    return rows


# ---------------------------------------------------------------- OCR

def _to_gray_box(crop: np.ndarray, height: int = 48, border: int = 8) -> np.ndarray:
    """红底白字 -> 白底黑字，并统一高度，喂给识别模型。返回单通道图。"""
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
    inv = cv2.bitwise_not(gray)
    scale = height / max(1, inv.shape[0])
    inv = cv2.resize(inv, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return cv2.copyMakeBorder(inv, border, border, border, border,
                              cv2.BORDER_CONSTANT, value=255)


def _row_image(img: np.ndarray, row: list[tuple[int, int, int, int]],
               gap: int = 14, height: int | None = None) -> np.ndarray:
    """把整行方块拼成一张图 —— 识别模型看整行比看单字准得多。"""
    target_h = height or int(np.median([b[3] for b in row]))
    pieces: list[np.ndarray] = []
    for x, y, w, h in row:
        piece = _to_gray_box(img[y:y + h, x:x + w], height=target_h, border=0)
        pieces.append(piece)
        pieces.append(np.full((piece.shape[0], gap), 255, dtype=np.uint8))
    canvas = np.hstack(pieces[:-1])
    return cv2.copyMakeBorder(canvas, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)


def _rec(img: np.ndarray) -> tuple[str, float]:
    """只做识别（不检测），返回 (文本, 置信度)。"""
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    out = _get_engine()(img, use_det=False, use_cls=False, use_rec=True)
    txts = getattr(out, "txts", None) or ()
    scores = getattr(out, "scores", None) or ()
    if not txts:
        return "", 0.0
    text = "".join(txts)
    conf = float(np.mean(scores)) if scores else 0.0
    return _clean(text), conf


def _rec_row(img: np.ndarray, row: list[tuple[int, int, int, int]]) -> tuple[str, float]:
    """整行识别 + 多尺度投票，压住单个字被认错的情况。"""
    results: list[tuple[str, float]] = []
    for height in (40, 64, 96):
        text, conf = _rec(_row_image(img, row, height=height))
        if text:
            results.append((text, conf))
    if not results:
        return "", 0.0
    votes: dict[str, list[float]] = {}
    for text, conf in results:
        votes.setdefault(text, []).append(conf)
    best = max(votes.items(), key=lambda kv: (len(kv[1]), sum(kv[1]) / len(kv[1])))
    return best[0], float(sum(best[1]) / len(best[1]))


def _clean(text: str) -> str:
    for ch in PUNCT:
        text = text.replace(ch, "")
    return text.strip()


def _consistent(row: list[tuple[int, int, int, int]]) -> bool:
    """口令方块大小一致；宽窄差太多的多半是几个不相干的红色元素凑在一起。"""
    widths = [b[2] for b in row]
    heights = [b[3] for b in row]
    return max(widths) / min(widths) <= 1.7 and max(heights) / min(heights) <= 1.7


def extract_code(img: np.ndarray, debug_path: str | None = None) -> CodeHit | None:
    """从一张截图里提取口令；找不到返回 None。"""
    scale = 1.0
    if img.shape[1] > MAX_WORK_WIDTH:
        scale = MAX_WORK_WIDTH / img.shape[1]
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

    rows = [r for r in group_rows(find_boxes(img))
            if len(r) >= MIN_ROW_BOXES and _consistent(r)]
    if not rows:
        rows = _split_merged_band(img)
    if not rows:
        return None
    row = max(rows, key=len)

    code, conf = _rec_row(img, row)
    if not code:  # 整行失败时，逐字识别兜底
        chars = [_rec(_to_gray_box(img[y:y + h, x:x + w]))[0] for x, y, w, h in row]
        code = "".join(c for c in chars if c)
        conf = 0.0
    if not code or len(code) < 2:
        return None

    xs = [b[0] for b in row]
    ys = [b[1] for b in row]
    x0, y0 = min(xs), min(ys)
    x1 = max(b[0] + b[2] for b in row)
    y1 = max(b[1] + b[3] for b in row)
    if scale != 1.0:  # 还原成原始屏幕坐标
        x0, y0, x1, y1 = (int(v / scale) for v in (x0, y0, x1, y1))

    if debug_path:
        vis = img.copy()
        for b in row:
            cv2.rectangle(vis, (b[0], b[1]), (b[0] + b[2], b[1] + b[3]), (0, 255, 0), 2)
        cv2.rectangle(vis, (x0, y0), (x1, y1), (255, 0, 0), 3)
        os.makedirs(os.path.dirname(debug_path) or ".", exist_ok=True)
        cv2.imwrite(debug_path, vis)

    return CodeHit(code=code, chars=list(code), confidence=conf, rect=(x0, y0, x1 - x0, y1 - y0))


def scan_screen(monitor: int = 1, region: tuple[int, int, int, int] | None = None) -> CodeHit | None:
    img = capture_region(region, monitor) if region else capture(monitor)
    return extract_code(img)


def main() -> None:
    ap = argparse.ArgumentParser(description="口令红包识别自检")
    ap.add_argument("--image", help="识别本地图片")
    ap.add_argument("--screen", action="store_true", help="识别当前屏幕")
    ap.add_argument("--region", help="只识别屏幕区域 x,y,w,h")
    ap.add_argument("--monitor", type=int, default=1)
    ap.add_argument("--debug", action="store_true", help="输出标注图 debug/last_scan.png")
    args = ap.parse_args()

    region = None
    if args.region:
        region = tuple(int(v) for v in args.region.split(","))  # type: ignore[assignment]

    started = time.time()
    if args.image:
        img = cv2.imread(args.image)
        if img is None:
            raise SystemExit(f"读不到图片: {args.image}")
    elif args.screen:
        img = capture_region(region, args.monitor) if region else capture(args.monitor)
    else:
        ap.print_help()
        return

    hit = extract_code(img, debug_path="debug/last_scan.png" if args.debug else None)
    cost = (time.time() - started) * 1000
    if hit:
        print(f"口令 = {hit.code}")
        print(f"置信度 = {hit.confidence:.2f}   区域 = {hit.rect}   耗时 = {cost:.0f}ms")
    else:
        print(f"未识别到口令（耗时 {cost:.0f}ms）")


if __name__ == "__main__":
    main()
