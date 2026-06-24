# -*- coding: utf-8 -*-
import subprocess, os, time
from rapidocr_onnxruntime import RapidOCR

ADB = r"D:\Android\platform-tools\adb.exe"
os.chdir(r"D:\桌面\抖音数据抓取(非影刀)")

print("[1] am start livelite ...")
subprocess.run([ADB, "shell", "am", "start", "-n",
  "com.ss.android.ugc.livelite/com.ss.android.ugc.aweme.splash.SplashActivity"],
  capture_output=True)
time.sleep(8)

print("[2] screencap ...")
subprocess.run([ADB, "shell", "screencap", "-p", "/sdcard/o.png"], capture_output=True)
subprocess.run([ADB, "pull", "/sdcard/o.png", "ocr_test.png"], capture_output=True)
subprocess.run([ADB, "shell", "rm", "/sdcard/o.png"], capture_output=True)
print("    size:", os.path.getsize("ocr_test.png"))

print("[3] RapidOCR ...")
ocr = RapidOCR()
result, _ = ocr("ocr_test.png")
print("    found", len(result), "blocks:")
for box, text, score in result[:25]:
    cx = int((box[0][0]+box[2][0])/2)
    cy = int((box[0][1]+box[2][1])/2)
    print(f"    '{text}' @({cx},{cy}) {score:.2f}")
print("[done]")
