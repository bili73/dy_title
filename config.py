# -*- coding: utf-8 -*-
"""
config.py
================================================================================
抖音商城(livelite)「拍同款」抓取 - 全局配置（adb + RapidOCR 方案）
================================================================================
放弃 Appium（livelite 是 Flutter 应用，UiAutomator 读其元素树必崩），改用：
  adb 截图/点击/滑动/启动 + RapidOCR 识别文字坐标 + OpenCV 模板匹配找图标。

集中管理：adb 路径、设备、livelite 包名/Activity、图片路径、抓取参数、输出、模板。
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ==============================================================================
# 1. adb 配置
# ==============================================================================
ADB_CONFIG = {
    "adb_path": r"D:\Android\platform-tools\adb.exe",   # adb 绝对路径（已确认）
    "udid": "10AG5603GY007KL",                          # 设备序列号（adb devices 查到）
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
DETAIL_CONFIG = {
    # 详情页下滑查找「参数容器」标签(产品参数/规格)的最大滑动次数
    "max_scrolls_find_params": 8,
    # 进入详情页 / 点击参数容器后等待界面稳定秒数
    "detail_settle_seconds": 3.0,
    # 参数网格：value 行在 key 行「上方」，两行 cy 差(像素)落在该区间才视为一组
    "param_row_dy": (50, 120),
    # 同一文本行聚类用的 cy 容差(像素)
    "param_row_cy_tol": 35,
    # key-value 配对时允许的 cx(水平) 距离容差(像素)
    "param_cx_tol": 200,
    # 一行至少含 N 个文本项才可能是参数网格行(过滤单行标题/价格/销量)
    "param_min_items_per_row": 2,
    # 完整标题：在价格下方该 cy 偏移区间内寻找候选标题行
    "title_search_dy": (100, 700),
    # 候选标题行合并：相邻行 cy 差小于该值视为同一标题的多行
    "title_merge_dy": 120,
    # 候选标题行最短字数(过滤短噪音)
    "title_min_chars": 6,
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
# 5. 模板匹配（定位无文字的图标按钮，如相机/搜索/返回）
# ==============================================================================
TEMPLATE_CONFIG = {
    # 小图标模板图片放此目录，命名如 camera.png / search.png / back.png
    "template_dir": os.path.join(BASE_DIR, "templates"),
    "match_threshold": 0.75,              # OpenCV matchTemplate 阈值(0~1，越大越严)
}
