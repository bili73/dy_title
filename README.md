# 抖音商城「拍同款」抓取（adb + PaddleOCR）

用 **adb + PaddleOCR** 操控真机上的抖音商城 App（`com.ss.android.ugc.livelite`），
自动走「拍同款 / 图片搜索」，抓取同款商品的**标题、价格、店铺**；进详情页抓取
**完整标题 + 完整参数**（列表页标题被截断，参数在详情页专门容器中需下滑才出现）。

> **为什么不用 Appium？** livelite 是 Flutter/自绘应用，Appium UiAutomator2 读其
> 元素树必崩。改用 **adb 截图/点击/滑动 + PaddleOCR 识别文字坐标** 驱动，对 Flutter
> 界面稳定可用。

---

## 一、环境

| 组件 | 要求 | 备注 |
|------|------|------|
| Python | 3.12（uv 托管） | `.python-version` |
| uv | 包管理 | `uv sync` 装依赖 |
| adb | platform-tools | exe 同目录或 `config.ADB_CONFIG.adb_path` |
| **OCR 服务** | PaddleOCR docker（PP-OCRv6 small，9300） | 见下「OCR 服务」；返回每行 4 角点 box |
| 真机 | USB 调试已开，`adb devices` 可见 | livelite **需先手动登录一次** |

---

## 二、安装

```powershell
uv sync                    # 装 Python 依赖
```

### OCR 服务（PaddleOCR docker）

抓取依赖 OCR HTTP 服务（`config.OCR_CONFIG`，默认 `http://localhost:9300/ocr`）：
返回 `{lines:[{text, box, score}]}`，box 为 4 角点（算文字中心坐标驱动点击）。

- **Docker 版**（本项目）：`D:\dev-services\paddleocr`（Dockerfile + app.py），
  `docker build -t dev-services-paddleocr .` + `docker run -d --name dev-paddleocr -p 9300:9300 dev-services-paddleocr`
- **无 docker 版**：见 `D:\桌面\抖音数据抓取_无docker`（OcrLocator 换 RapidOCR 本地，exe 打包即用）

---

## 三、运行

### CLI

```powershell
# 默认：只抓列表(标题/价格/店铺)
uv run python main.py

# 进详情抓完整标题 + 完整参数
uv run python main.py --detail --max-goods 5

# 临时覆盖搜索图 + 参数容器关键词(详情模式定位参数入口用)
uv run python main.py --detail --max-goods 3 --image "D:\xxx.jpg" --params-keywords "类型,速度级别"
```

### Web 前端（推荐，给运营用）

```powershell
uv run python -m web.server          # 启动后自动开浏览器 http://localhost:8010
```

前端功能：上传搜索图 / 选模式 / 设商品数 / **参数容器关键词** / Excel 目录 /
**结果文件名** / **相册首图校准**（换设备点选）/ 实时日志 + 截图 / 结果下载。

---

## 四、核心机制

### 参数容器关键词（进详情抓参数的关键）

详情页参数入口卡片（左图标 + 右参数摘要文字），图标样式多变（齿轮/列表/表盘）。
**靠关键词 OCR 定位**：前端填该品类的参数键（如轮胎 `类型,速度级别`，手机
`维修方式,上市时间`），OCR 命中参数摘要行 → 点击进完整参数页。第一个商品抓完后
关键词池**自动累积**（抓到的参数键并入池），后续同品类商品命中率更高。

### 完整参数配对（纯结构，去词典）

参数详情页布局：key 在最左一列（cx 小），value 在右（cx 大），同一行。
`collect_params_full` 纯位置配对（不依赖参数名词典，新品类零维护），兼容：
- **左右**（key 左 + value 右，同行）
- **上下网格**（value 上 + key 下，列 cx 对齐）
- **key 名续行**（OCR 把过长 key 名头部/尾部挤到独立行 → 找 cy 最近的 key 行合并）
- **一键多值**（同行多 value + 下方续行 value 归并）

### back 状态机

详情返回列表用状态机（按落点决定 back）：参数页/详情/直播（「说点什么」评论框）→
继续 back；列表（「找同款」标题）→ 停；其它 → 退过头兜底重进。

### 相册首图校准

`upload_image` 选首图坐标优先用运营校准值（per udid，`calibration.json`）；
换设备/相册改版后前端「校准相册首图」点选一次首图位置即可。

---

## 五、打包成 exe（给非技术人员）

```powershell
uv run pip install pyinstaller
uv run python build_exe.py          # → dist/抖音抓取/
```

打包后把 `paddleocr.tar`（`docker save` 导出镜像）+ `adb.exe(+dll)` 放进 dist 同目录，
压缩发同事。双击 exe → launcher 自动 `docker load` + run OCR + 启动 web + 开浏览器。

详见 `README.txt`（同事使用说明）+ `launcher.py`（启动器）。

---

## 六、项目结构

```
├── douyin_crawler.py    # 核心：AdbController + OcrLocator + TemplateMatcher + 流程
├── config.py            # 配置（adb/设备/App/详情坐标/输出/OCR/校准读写）
├── locators.py          # OCR 关键词 + 参数名提示词 + 卖点/噪音词
├── main.py              # CLI 入口 + save_results/save_excel（支持自定义文件名）
├── launcher.py          # exe 启动器（docker 检测/load/run + OCR 预热 + 开浏览器）
├── build_exe.py         # PyInstaller 打包脚本
├── web/                 # Web 前端（FastAPI + SSE）
│   ├── server.py        #   后端（/api/start, 校准, SSE 事件流）
│   └── static/index.html#   前端单页
├── images/              # 待搜索图片（sample.jpg）
├── output/              # 结果 JSON/TXT/Excel + 调试截图
└── calibration.json     # 设备校准数据（per udid，不进 git）
```

---

## 七、抓取结果示例（--detail 模式）

```json
[
  {
    "title": "一号ARISUN1系列...",
    "price": "¥999起", "coupon_price": "券后价¥894.1起", "shop": "官方旗舰店",
    "params": {
      "电压": "12V", "电源方式": "点烟器电源", "适用对象": "普通汽车轮胎",
      "电池容量": "5001MAh(含)-20000MAh(不含)", "功能": "应急辅助"
    }
  }
]
```

结果输出到 `output\`：`douyin_results.json/.txt/.xlsx`（Excel 每商品一行，参数合并到
「参数」列换行显示）。**文件名可自定义**（CLI `--output` 无；web 前端「结果文件名」，
留空覆盖模式用当前时间戳，追加模式用 douyin_results）。

---

## 八、调试（出问题第一动作）

看 `output\screenshots\shot_N.png`（每步截图）+ 控制台/web 日志 OCR：
- 找不到搜索/参数入口 → `locators.py` 关键词、`config.py` 坐标阈值（dp）
- 参数漏抓/配对错 → `config.py`（`param_key_cx_max`/`param_full_row_cy_tol`/`grid_*`）
- OCR 慢 → PaddleOCR 换 small 模型 + SCALE 缩图（`app.py`）
- 多设备 → `config.ADB_CONFIG.udid` 写死（默认 `auto` 取第一个）

详见 `CLAUDE.md`（项目架构 + 调试工作流 + 设计决策）。

---

## 九、合规与使用须知

- 仅供**学习、研究、个人自动化测试**使用。
- 自动化操作可能违反平台用户协议，请合法合规、控制频率、不损害平台与他人利益。
- 抓取数据请勿用于商业转售或侵权用途。
