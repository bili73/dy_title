# 抖音商城「拍同款」抓取（adb + RapidOCR）

用 **adb + RapidOCR** 操控真机上的抖音商城 App（`com.ss.android.ugc.livelite`），
自动走「拍同款 / 图片搜索」，抓取同款商品的**标题、价格、店铺**；
加 `--detail` 可逐个进入商品详情页，抓取**完整标题 + 完整参数**(列表页标题被截断，
参数在详情页专门网格容器中，需下滑才出现)。

> **为什么不用 Appium？**
> livelite 是 Flutter/自绘应用，Appium UiAutomator2 读取其元素树必崩（socket hang up，
> 经多版本 driver/server 验证）。改用 **adb 截图 + RapidOCR 识别文字坐标 + OpenCV 模板匹配**，
> 对 Flutter 界面稳定可用（已验证 RapidOCR 完美识别 livelite 商城首页 59 个文字块）。

---

## 一、环境

| 组件 | 位置 / 版本 | 备注 |
|------|-------------|------|
| Python | 3.12（uv 托管） | 见 `.python-version` |
| uv | `C:\Users\admin\.local\bin` | 包管理 |
| adb | `D:\Android\platform-tools` | 已在 config 配绝对路径 |
| 真机 | vivo V2136A (PD2136) / Android 16 | USB 调试已开 |
| 目标 App | 抖音商城 livelite | **需先手动登录一次** |

**不需要** Appium / Node / JDK / Appium Inspector。

---

## 二、安装依赖

```powershell
cd D:\桌面\抖音数据抓取(非影刀)
uv sync
```

---

## 三、准备图片

把待搜索图片放到 `images\sample.jpg`（或运行时 `--image` 指定）。建议清晰、主体居中。

---

## 四、运行

```powershell
# 默认：只抓列表(标题/价格/店铺)
uv run python main.py

# 进详情抓完整标题 + 完整参数(逐个进商品详情页 → 点参数容器进完整参数页 → 上滑收集全部参数)
uv run python main.py --detail --max-goods 5

# 指定图片：
uv run python main.py --detail --max-goods 3 --image "D:\some\shoe.jpg"
```

结果输出到 `output\`：
- `douyin_results.json` / `douyin_results.txt`：结构化数据 + 可读摘要
- `douyin_results.xlsx`：**Excel 表**(标题 + 价格 + 店铺 + 参数键值对，每商品一行)
- `screenshots\shot_N.png`：每步调试截图

---

## 五、真机调试（首次必做）

拍同款流程的入口、按钮文案、结果页布局需按真机实际界面校准：

| 文件 | 调什么 |
|------|--------|
| `config.py` | `adb_path`、`udid`、`scan_activity`（拍同款 Activity）、图片路径 |
| `locators.py` | OCR 关键词（搜索/相机/相册/确认/返回 等），按实际界面文案调 |
| `douyin_crawler.py` | `enter_scan`（进拍同款）、`upload_image`（选图坐标）、`collect_goods`（商品切分） |

**调试方法**：运行后看 `output\screenshots\shot_N.png`（每步截图）+ 控制台 OCR 日志，
确认文字识别准不准、点击坐标对不对，据此调整关键词/坐标。

关键流程点：
1. **进拍同款**：首页 OCR 找「搜索」按钮 → 点其左侧相机图标 → 进拍同款
2. **选图**：相册 → 点首图 → 完成（首图坐标用屏幕比例估算）
3. **进详情(避直播)**：只点列表**左半屏**商品(cx &lt; 屏宽/2)，避开卡片右下角直播入口；进详情后**等待 4 秒**+价格出现稳定，再操作（页面有直播浮窗，急点易误进直播）
4. **完整标题/价格/店铺**：详情页第0屏(不下滑)即含完整标题+原价+券后价+店铺
5. **完整参数**：下滑找「退货包邮券·7天无理由退货」锚点 → 点其上方参数容器进**完整参数页** → **上滑**收集全部参数(15~22 项键值对)

---

## 六、项目结构

```
抖音数据抓取(非影刀)/
├── pyproject.toml / uv.lock / .python-version   # uv 环境
├── config.py            # 配置（adb/设备/App/图片/输出/模板）
├── locators.py          # OCR 关键词
├── douyin_crawler.py    # 核心：AdbController + OcrLocator + TemplateMatcher + 流程
├── main.py              # 运行入口
├── test_ocr.py          # OCR 识别验证脚本（调试用）
├── images/              # 待搜索图片（sample.jpg）
├── templates/           # 图标模板（可选，用于模板匹配找相机/搜索图标）
└── output/              # 结果 JSON/TXT + 调试截图
```

---

## 七、抓取结果示例（--detail 模式）

```json
[
  {
    "title": "SANC盛色27英寸2K200Hz电竞显示器IPS硬件低蓝光G72Max增强版",
    "price": "券后价￥729",
    "coupon_price": "券后价￥729",
    "shop": "抖音旗舰",
    "params": {
      "是否触摸屏": "否",
      "屏幕尺寸": "27",
      "刷新率": "200hz",
      "分辨率": "2560*1440",
      "面板类型": "FAST-IPS",
      "品牌": "SANC/盛色",
      "型号": "G72Max增强版",
      "重量": "7.22kg",
      "CCC证书编号": "2024010903638619",
      "生产企业名称": "宜宾佳信电子科技有限公司",
      "保修期": "3年"
    }
  }
]
```

> 完整参数共 15~22 项(点参数容器进完整参数页 + 上滑收集)。

Excel 表 (`douyin_results.xlsx`)：每商品一行，参数合并到「参数」列(换行显示 `key: value`)。

---

## 八、常见问题

- **`未抓取到商品`**：看 `output\screenshots\` 截图，确认拍同款是否真进了、结果页 OCR 是否识别到价格。
- **点错位置**：`upload_image` 的首图坐标是估算，按截图实际调整。
- **OCR 漏识别**：调 `locators.py` 关键词，或降 `config.ocr_score_threshold`。
- **adb 找不到**：确认 `config.ADB_CONFIG.adb_path` 绝对路径正确，`adb devices` 能看到设备。
- **拍同款进不去**：手动进一次拍同款，看 `dumpsys activity activities` 的 Activity 名，更新 `scan_activity`。

---

## 九、合规与使用须知

- 仅供**学习、研究、个人自动化测试**使用。
- 自动化操作可能违反平台用户协议，请合法合规、控制频率、不损害平台与他人利益。
- 抓取数据请勿用于商业转售或侵权用途。
