# -*- coding: utf-8 -*-
import os, sys, time
sys.path.insert(0, r"D:\桌面\抖音数据抓取(非影刀)")
os.chdir(r"D:\桌面\抖音数据抓取(非影刀)")
from douyin_crawler import AdbController, OcrLocator
import config, locators

adb = AdbController(config.ADB_CONFIG["adb_path"], config.ADB_CONFIG["udid"])
ocr = OcrLocator()
shot_dir = config.OUTPUT_CONFIG["screenshot_dir"]
def shot_ocr(tag): return ocr.recognize(adb.screencap(os.path.join(shot_dir, tag)))

# force-stop 强制回首页
adb.shell("input keyevent 224"); time.sleep(0.5)
adb.shell(f"am force-stop {config.APP_CONFIG['package']}"); time.sleep(1)
adb.am_start(f"{config.APP_CONFIG['package']}/{config.APP_CONFIG['launch_activity']}")
time.sleep(8)

# 等首页"搜索"
anchor = None
for i in range(12):
    items = shot_ocr(f"h{i}.png")
    anchor = ocr.find_text(items, ["搜索"])
    if anchor: print(f"首页OK 第{i+1}次 '搜索'@{int(anchor['cx'])},{int(anchor['cy'])}"); break
    time.sleep(1.5)
if not anchor:
    print("仍未到首页，当前:", [it["text"] for it in items[:12]]); sys.exit(0)

# 点相机→拍同款
adb.tap(anchor["left"]-90, anchor["cy"]); time.sleep(4)
# 点相册
items2 = shot_ocr("scan.png")
alb = ocr.find_text(items2, locators.ALBUM)
if not alb: print("无相册:", [it["text"] for it in items2[:12]]); sys.exit(0)
print(f"点相册 '{alb['text']}' @({int(alb['cx'])},{int(alb['cy'])})")
adb.tap(alb["cx"], alb["cy"]); time.sleep(4)

# 相册页布局
items3 = shot_ocr("album.png")
print(f"相册页 {len(items3)} 块:")
for it in items3[:18]:
    print(f"    '{it['text']}' @({int(it['cx'])},{int(it['cy'])}) {it['score']:.2f}")
print("确认类按钮:")
for kw in locators.CONFIRM + ["图片","最近","完成"]:
    f = ocr.find_text(items3,[kw])
    if f: print(f"    ✓ '{kw}' @({int(f['cx'])},{int(f['cy'])})")
