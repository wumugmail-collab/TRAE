# 任务交接说明

## 当前主任务
做一款「支付宝口令红包」监测工具：周期性截屏 → 本地 OCR → 自动提取红包口令 → 桌面通知 + 复制到剪贴板。目标平台为 Windows 桌面，仅用户自用，不分发。

## 文件清单
- `redpacket_watcher.py` —— 核心工具，**已完成**，检测规则已按用户描述调优并通过 10 组样例测试。
- `bsc_query.py` —— BSC 链上只读查询（早期任务遗留，不需要可忽略）。
- `wallet-connect.html` —— 钱包连接调试页（遗留，不要继续；币安因沙箱出口在北京被地域封锁，已放弃该方向）。

## redpacket_watcher.py 现状
- 依赖：`pip install -U rapidocr-onnxruntime mss pillow numpy pyperclip plyer`
- 用法：
  - `python redpacket_watcher.py --once 图片.png`　单图调试（验证 OCR + 提取）
  - `python redpacket_watcher.py --region X Y W H`　实时监测指定区域
- 口令特征（用户口述）：
  - 长度 > 5，常见 6～20，最长可到十几个字符
  - 主体「数字 + 中文」，少量含英文字母
  - 极少带符号
- 提取逻辑：
  - 优先匹配「口令 / 口令： / 口令是 / 口令为」标签后紧跟的短语
  - 兜底逐行扫描符合特征的短语
  - `_looks_like_kouling()` 校验：长度 6~20，且中英数占比 ≥ 70%，含标签词（支付宝/红包/口令/输入/领取等）则排除
- 已用 10 组样例验证，全部符合预期。

## 待办 / 下一步
1. 用户上传真实红包图后，跑 `--once` 验证 OCR 识别率，再按真实排版（可能竖排/水印等）继续调优。
2. 图片接收问题（重要）：当前语音会话里用户发的图走「视觉理解管线」，报 `Invalid image`，从未落到 `/workspace/.uploads`。建议用户：**新建任务 + 以文件上传 + 优先发截图（PNG）而非原图（可能是 HEIC/实况照片）**。
3. 可选项：打包成 exe（个人自用免签名）；加区域选择 GUI。