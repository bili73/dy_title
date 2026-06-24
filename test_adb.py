# -*- coding: utf-8 -*-
import os, sys
sys.path.insert(0, r"D:\桌面\抖音数据抓取(非影刀)")
os.chdir(r"D:\桌面\抖音数据抓取(非影刀)")
from douyin_crawler import AdbController
import config

adb = AdbController(config.ADB_CONFIG["adb_path"], config.ADB_CONFIG["udid"])
shot_dir = config.OUTPUT_CONFIG["screenshot_dir"]
os.makedirs(shot_dir, exist_ok=True)
path = os.path.join(shot_dir, "dbg2.png")
try:
    adb.screencap(path)
    print("OK exists:", os.path.exists(path), "size:", os.path.getsize(path))
except Exception as e:
    print("FAIL:", e)
