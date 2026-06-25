# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

抖音商城（`com.ss.android.ugc.livelite`，独立电商 App）「拍同款 / 图片搜索」自动化抓取：把本地图片推到真机相册 → 走拍同款搜同款商品 → 抓取**标题、价格、店铺**；`--detail` 模式逐个进详情页抓取**完整标题 + 完整参数**（列表页标题被截断，参数在详情页专门容器中需下滑才出现）。

## 常用命令

```powershell
uv sync                                          # 安装依赖（uv 托管，Python 3.12）
uv run python main.py                            # 默认：只抓列表（标题/价格/店铺）
uv run python main.py --detail --max-goods 5     # 进详情抓完整标题 + 完整参数
uv run python main.py --detail --image "D:\path\xxx.jpg"   # 临时覆盖待搜索图片

uv run python test_paddle.py                     # 跑某个调试脚本（见下）
```

- **无 lint / 无正式测试套件**：根目录的 `test_*.py` 全是**一次性真机调试脚本**（手动跑、看输出/截图），不是 pytest，也未纳入 git。要验证某个流程，照着现有 `test_*.py` 的写法（`sys.path.insert + from douyin_crawler import ...`）新建一个即可。
- 重启前按用户全局规范：先确认 adb 设备在线、OCR 服务在跑，再启 `main.py`。

## 运行前置依赖（缺一不可，否则必崩）

1. **真机**：USB 调试已开，`adb devices` 能看到；livelite **需先手动登录一次**。当前设备/udid 写死在 `config.ADB_CONFIG`。
2. **adb**：绝对路径在 `config.ADB_CONFIG.adb_path`（`D:\Android\platform-tools\adb.exe`）。
3. **OCR HTTP 服务**：`OcrLocator` 依赖 `config.OCR_CONFIG.paddleocr_url`（默认 `http://localhost:9300/ocr`，PaddleOCR PP-OCRv6，docker 部署）。**服务没跑 = 抓取直接失败**。服务必须返回 `{lines:[{text, box, score}]}`，`box` 为 4 角点（用来算文字中心坐标）；只返回纯文字字符串的旧格式会被跳过（见 `OcrLocator.recognize`）。
4. 待搜索图片：默认 `images\sample.jpg`（或 `--image`）。

## 核心架构

**为什么不用 Appium**：livelite 是 Flutter/自绘应用，Appium UiAutomator2 读其元素树必崩（socket hang up，经多版本验证）。方案改为 **adb 截图/点击/滑动 + OCR 识别文字坐标 + OpenCV 模板匹配找无文字图标**。

`douyin_crawler.py` 内 4 个类，职责清晰、勿混：

| 类 | 职责 |
|----|------|
| `AdbController` | 封装 adb：截图、`input tap/swipe`、`am start`、`push`、`dumpsys` 查前台。所有设备 I/O 都走它。 |
| `OcrLocator` | 调 OCR 服务，把返回的 `box` 算成 `{text, cx, cy, top, bottom, left, right, score}`；`find_text`/`find_prices` 做关键词/价格定位。 |
| `TemplateMatcher` | OpenCV `matchTemplate`（Canny 边缘匹配）定位**无文字图标**（如参数入口的表盘图标 `param_icon*.png`），抗颜色/亮度/缩放差异。 |
| `DouyinCrawler` | 流程编排：`start_app → enter_scan → push_image → upload_image → scroll_and_collect / run_detail`。 |

**核心设计原则**：用 OCR 文字 bbox 中心坐标驱动点击，**绝不硬编码坐标**（少数首图/完成按钮等无法 OCR 的才用屏幕比例估算，注释里都标了「需按实际校准」）。

两条主流程入口（`main.py` 据参数二选一）：
- `run()`：只抓列表页（`scroll_and_collect` → `collect_goods`）。
- `run_detail(max_goods)`：到列表后**每轮重新 OCR 当前屏**，按「未抓过的价格」选下一个商品进详情（避免详情返回后列表重绘致坐标失效/重复进同款）。

## ⚠️ 文档与代码的 OCR 矛盾（务必知晓）

README、各文件 docstring 都说用 **RapidOCR**，`douyin_crawler.py:26` 也 `from rapidocr_onnxruntime import RapidOCR`——**但这个 import 实际没被使用，是遗留代码**。`OcrLocator` 真正用的是 **PaddleOCR HTTP 服务**（`requests.post` 到 `paddleocr_url`）。改 OCR 相关逻辑时认 `OcrLocator` 的实现，别被「RapidOCR」字样误导。同理 `pyproject.toml` 仍列着已弃用的 `appium-python-client`/`selenium`，可清理但非必需。

## 详情页抓取流程（`--detail`，最复杂、最易出错）

布局规律（真机验证，写进了 `config.DETAIL_CONFIG` 注释）：

1. **进详情避直播**：只点列表**左半屏**商品（`cx < w*0.5`，避开卡片右下直播入口），进详情后**等 4 秒**（页面有直播浮窗，急点易误进直播）。
2. **第 0 屏抓标题/价格/店铺**：进详情**不下滑**，完整标题 + 原价 + 券后价 + 店铺都在第一屏。若先下滑找参数，标题会滚出屏顶（曾因此把卖点当标题）。`collect_detail` 严格按此顺序。
3. **完整参数**：下滑用**模板匹配**找参数入口图标（`param_icon*.png`，`find_params_entry`）→ 点图标进完整参数页 → **上滑多次收集**（`collect_full_params_page`，按 key 去重合并）→ `back()` 关闭。

参数配对算法（`collect_params` / `collect_params_full`）依赖详情页网格布局：「value 行在上 + key 行在下」，同行 cx 接近。调参改 `config.DETAIL_CONFIG`（`param_row_dy`/`param_cx_tol`/`title_search_dy` 等），别改算法常量。

## 调试工作流（出问题时的第一动作）

1. 看 `output\screenshots\shot_N.png`（`_shot()` 每步截图，序号递增）+ 控制台 OCR 日志，确认**文字识别准不准、点击坐标对不对**。
2. 拍同款入口/选图/结果页布局变化时，按文件分工调：
   - `config.py`：adb 路径、`udid`、`scan_activity`、各坐标阈值/等待秒数。
   - `locators.py`：OCR 关键词（界面文案变了就调这里）、参数名提示词 `PARAM_KEY_HINTS`、噪音词 `TITLE_NOISE_KEYWORDS`/`PARAM_SELLER_WORDS`。
   - `douyin_crawler.py`：流程步骤本身的启发式（`enter_scan`/`upload_image`/`collect_*`）。

## Windows / 中文路径约束（已固化在代码里，勿回退）

- `AdbController.screencap` 用 `adb exec-out screencap -p` 直接拿 PNG 字节流由 Python 写文件——**刻意避开 `adb pull`**（adb 解析中文路径会乱码）。
- `TemplateMatcher._imread` 用 `np.fromfile + cv2.imdecode`——`cv2.imread` 在 Windows 不支持中文路径（返回 None）。
- 所有文件操作一律用**完整绝对 Windows 路径**（带盘符 + 反斜杠），符合用户全局规范。
- Excel 被占用时 `save_excel` 自动存带时间戳备选名，不崩。

## 输出

`output\`：`douyin_results.json` + `.txt`（可读摘要）+ `.xlsx`（每商品一行，参数合并到「参数」列换行显示）；`output\screenshots\` 存调试截图。`main.py:save_results` 会从 JSON 去掉调试用 `box` 字段。
