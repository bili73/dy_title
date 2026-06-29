# -*- coding: utf-8 -*-
"""
config.py
================================================================================
抖音商城(livelite)「拍同款」抓取 - 全局配置（adb + RapidOCR 方案）
================================================================================
放弃 Appium（livelite 是 Flutter 应用，UiAutomator 读其元素树必崩），改用：
  adb 截图/点击/滑动/启动 + RapidOCR 识别文字坐标 + OpenCV 模板匹配找图标。

集中管理：adb 路径、设备、livelite 包名/Activity、图片路径、抓取参数、输出、模板。

设备分辨率/DPI 适配：
  凡是「相对锚点的像素偏移」和「配对容差/区间」一律用 **dp**（density-independent
  pixel）表达，运行时由 AdbController.dp() 按设备实际 dpi 换算成 px：
      px = round(dp * dpi / 160)
  基准设备 1080x2376 @ 480dpi（1dp=3px）。DETAIL_CONFIG 中带坐标语义的字段、以及
  LIST_OFFSETS_DP 的值，单位均为 dp（注释里标注 [dp]）。换分辨率/DPI 的手机自动适配，
  无需改代码。与坐标无关的字段（秒数/次数/字数/置信度）保持原单位。
  设备 udid 设 "auto" 时由 adb devices 自动识别（见 AdbController._resolve_udid）。
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ==============================================================================
# 1. adb 配置
# ==============================================================================
ADB_CONFIG = {
    "adb_path": r"D:\Android\platform-tools\adb.exe",   # adb 绝对路径（已确认）
    # "auto" = 自动取 `adb devices` 第一个在线设备；想锁死某台就填具体序列号
    # （如原写死值 "1551169392ZZZZZ"，vivo V2136A / PD2136）
    "udid": "auto",
    "device_screenshot": "/sdcard/dy_shot.png",         # 设备端临时截图文件
    "cmd_timeout": 30,                                  # 单条 adb 命令超时(秒)
}


# ==============================================================================
# 2. 目标 App：抖音商城 livelite（独立电商 App，区别于抖音主 App aweme）
# ==============================================================================
APP_CONFIG = {
    "package": "com.ss.android.ugc.livelite",
    # 启动入口 Activity
    "launch_activity": "com.ss.android.ugc.aweme.splash.SplashActivity",
    # 商城首页 Activity（商城首页就在 SplashActivity 内渲染，已验证）
    "home_activity": "com.ss.android.ugc.aweme.splash.SplashActivity",
    # 拍同款/扫码搜商品 Activity（影刀曾用它进入拍同款，已确认存在）
    "scan_activity": "com.ss.android.ugc.aweme.qrcode.ecom.ScanCommodityActivity",
    # 本地图片推送到手机的相册目录（拍同款选图用）
    "device_image_dir": "/sdcard/DCIM/Camera/",
}


# ==============================================================================
# 3. 抓取行为
# ==============================================================================
CRAWL_CONFIG = {
    "search_image_path": os.path.join(BASE_DIR, "images", "sample.jpg"),
    "max_scroll_times": 8,                # 结果页最多上滑加载次数
    "results_limit": 30,                  # 最多抓取商品条数（去重后）
    "settle_seconds": 2.0,                # 每次操作后等待界面稳定秒数
    "swipe_duration_ms": 600,             # 单次滑动手势耗时(ms)
    "ocr_score_threshold": 0.6,           # OCR 置信度过滤阈值
    "enter_detail": False,                # 是否进详情抓参数（Flutter 详情 OCR 可能不全，默认关）
}


# ==============================================================================
# 4. 详情页抓取参数（enter_detail=True 时用）
# ==============================================================================
# 真机验证(livelite 详情页)得出的布局规律：
#   - 完整标题在「价格下方」一段（列表页标题被截断，详情页才有全文）
#   - 参数在一个专门容器里，需下滑才能看到其入口标签(产品参数/规格)
#   - 点开容器后参数以「网格」排列：每个单元格 value(上行) + key(下行)，
#     value 在 key 上方约 80px，同单元格 key/value 的 cx 接近
# ⚠️ 下面带「距离/容差」语义的字段单位为 **dp**，运行时由 AdbController.dp() 按设备
#    实际 dpi 换算 px（基准 480dpi 下 1dp=3px）。换机自动适配。括号内标注基准像素值
#    供对照。与坐标无关的字段（秒数/次数/字数）保持原单位。
DETAIL_CONFIG = {
    # 详情页下滑查找「参数容器」标签(产品参数/规格)的最大滑动次数
    "max_scrolls_find_params": 8,
    # 进入详情页 / 点击参数容器后等待界面稳定秒数
    "detail_settle_seconds": 3.0,
    # 参数网格：value 行在 key 行「上方」，两行 cy 差区间 [dp]（基准≈(50,120)px）
    "param_row_dy": (17, 40),
    # 同一文本行聚类用的 cy 容差 [dp]（基准≈35px）
    "param_row_cy_tol": 12,
    # key-value 配对时允许的 cx(水平) 距离容差 [dp]（基准≈200px）
    "param_cx_tol": 67,
    # 一行至少含 N 个文本项才可能是参数网格行(过滤单行标题/价格/销量)
    "param_min_items_per_row": 2,
    # 完整标题：在价格下方该 cy 偏移区间内寻找候选标题行 [dp]（基准≈(100,700)px）
    "title_search_dy": (33, 233),
    # 候选标题行合并：相邻行 cy 差小于该值视为同一标题的多行 [dp]（基准≈120px）
    "title_merge_dy": 40,
    # 候选标题行最短字数(过滤短噪音)
    "title_min_chars": 6,
    # —— 完整参数页 key-value 配对容差（collect_params_full 用）[dp] ——
    "full_kv_same_row_dy": 15,          # 同行右侧：value 与 key 的垂直容差(原 45px)
    "full_kv_same_row_dx": (17, 267),   # 同行右侧：value 在 key 右侧的水平距离区间(原 50~800px)
    "full_kv_above_dy": (-43, -10),     # 正上方：value 在 key 上方的垂直距离区间(原 -130~-30px)
    "full_kv_above_dx": 87,             # 正上方：value 与 key 的水平容差(原 260px)
    # 完整参数页 key 列 cx 上限 [dp]：key 固定在最左一列(cx 小)，value 在右(cx 大)。
    # collect_params_full 纯结构配对用——行内首项 cx < 此值即为 key，否则当作续行 value。
    # 基准 480dpi 下 60dp≈180px(轮胎 key cx 93~131px，value cx 345+px，分界 180 安全)。
    "param_key_cx_max_dp": 60,
    # collect_params_full 同行聚类 cy 容差 [dp]：详情页 key+value 同行 cy 差极小(<5px)，
    # 用比 param_row_cy_tol 更小的值，避免 key 名尾部续行(cy 差约 35~44px)被误聚进上一行
    # (否则续行"素"会和 key"后置摄像头像"挤成同行、cx 最小被当 key)。
    "param_full_row_cy_tol_dp": 7,
}


# ==============================================================================
# 4.1 列表页/通用定位偏移（单位 dp，运行时按设备 dpi 换算 px）
# ==============================================================================
# 这些偏移原散落在 enter_scan/collect_goods 方法体里写死成像素，现集中于此、用 dp
# 表达，换机自动适配。基准 480dpi(1dp=3px) 下与原像素值误差 ≤3px（容差/区间型无损）。
LIST_OFFSETS_DP = {
    "camera_left": 30,      # 相机入口在搜索按钮左侧的偏移（原 90px）
    "title_above_dy": 7,    # 标题在价格上方的垂直容差（原 20px）
    "title_right_dx": 67,   # 标题相对价格右侧的水平容差（原 200px）
    "shop_cy_tol": 100,     # 店铺与价格的垂直距离容差（原 300px）
}


# ==============================================================================
# 5. 结果输出
# ==============================================================================
OUTPUT_CONFIG = {
    "output_dir": os.path.join(BASE_DIR, "output"),
    "output_file": "douyin_results.json",
    "save_txt_summary": True,
    "txt_file": "douyin_results.txt",
    "excel_file": "douyin_results.xlsx",   # Excel 输出(标题 + 参数键值对)
    "screenshot_dir": os.path.join(BASE_DIR, "output", "screenshots"),  # 调试用截图存这
}


# ==============================================================================
# 6. 模板匹配（定位无文字的图标按钮，如相机/搜索/返回/参数入口图标）
# ==============================================================================
TEMPLATE_CONFIG = {
    # 小图标模板图片放此目录，命名如 camera.png / search.png / back.png / param_icon.png
    "template_dir": os.path.join(BASE_DIR, "templates"),
    "match_threshold": 0.60,             # OpenCV matchTemplate 阈值(0~1，越大越严)
}


# ==============================================================================
# 7. OCR 服务(PaddleOCR PP-OCRv6，docker 部署)
# ==============================================================================
# POST multipart field 'file'=<image>，返回 {count, text, lines:[{text, box, score}]}
# box 为 4 个角点 [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]，用于算文字中心坐标驱动点击/配对。
OCR_CONFIG = {
    "paddleocr_url": "http://localhost:9300/ocr",
    "timeout": 120,                      # 单次 OCR 请求超时(秒)；PaddleOCR CPU 推理较慢
}
