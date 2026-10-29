# 任务交接说明

## 当前主任务
做一款「支付宝口令红包」监测工具：周期性截屏 → 本地 OCR → 自动提取红包口令 → 桌面通知 + 复制到剪贴板。目标平台为 Windows 桌面，仅用户自用，不分发。

## 代码所在分支（重要）
所有代码在 `trae/agent-pQLFxU` 分支；`main` / `use-for-bian` 只有 README。
新会话先执行：
```
git fetch origin && git checkout -B trae/agent-pQLFxU origin/trae/agent-pQLFxU
```
（默认 clone 的 fetch refspec 只跟踪 use-for-bian，可能需要手动 fetch 该分支）

## 文件清单
- `redpacket_watcher.py` —— 核心工具。
- `bsc_query.py` —— BSC 链上只读查询（早期任务遗留，不需要可忽略）。
- `wallet-connect.html` —— 钱包连接调试页（遗留，不要继续；币安因沙箱出口在北京被地域封锁，已放弃该方向）。

## redpacket_watcher.py 现状
- 依赖：`pip install -U rapidocr-onnxruntime mss pillow numpy pyperclip plyer`
- 用法：
  - `python redpacket_watcher.py --once 图片.png`　单图调试（验证 OCR + 提取）
  - `python redpacket_watcher.py --region X Y W H`　实时监测指定区域
- 提取逻辑：
  - 优先匹配「口令 / 口令是 / 复制 / 请复制」等提示词后的短语，遇到「打开支付宝 / 领取 / 点击 / 有效期」等引导语即截断（兼容 OCR 把空格吃掉，如「复制7xK9mP2打开支付宝」）
  - 兜底逐行扫描符合特征的短语
  - `_looks_like_kouling()` 校验：长度 6~20，中英数占比 ≥ 70%，排除标签词（支付宝/红包/口令/输入/领取/打开…）与界面噪声词（有效期/小时/余额/已领/金额…）
  - 实时监测中同一个口令只通知一次，避免画面抖动反复弹窗
  - 剪贴板库缺失时降级为仅打印+通知，不崩溃

## 验证情况
- 已在沙箱内用 Python 3.12 + rapidocr-onnxruntime 跑通完整链路（OCR 模型可正常加载）。
- 3 张合成截图（口令「恭喜发财888」、「复制7xK9mP2打开支付宝」、「口令是秋天第一杯奶茶」）OCR + 提取全部正确。
- 噪声页面（「有效期24小时」「余额12.34元」）正确返回空，不再误报。
- 14 组字符串回归用例全部通过（含纯口令、「请复制口令」、无口令页面等）。
- 测试图与测试脚本在 `/tmp/rptest`，未入库。

## 待办 / 下一步
1. 用户上传真实红包图后，跑 `--once` 验证 OCR 识别率，再按真实排版（可能竖排/水印等）继续调优。合成图无法覆盖真实字体、水印、缩放。
2. 图片接收问题（重要）：语音会话里用户发的图走「视觉理解管线」，报 `Invalid image`，从未落到 `/workspace/.uploads`。建议用户：**新建任务 + 以文件上传 + 优先发截图（PNG）而非原图（可能是 HEIC/实况照片）**。
3. 可选项：打包成 exe（个人自用免签名）；加区域选择 GUI。
