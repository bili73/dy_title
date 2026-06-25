# -*- coding: utf-8 -*-
import os, sys
sys.path.insert(0, r"D:\桌面\抖音数据抓取(非影刀)")
os.chdir(r"D:\桌面\抖音数据抓取(非影刀)")
from douyin_crawler import OcrLocator
ocr = OcrLocator()
items = ocr.recognize(r"D:\桌面\抖音数据抓取(非影刀)\entry2.png")
print(f"识别 {len(items)} 项(带坐标)")
for it in items[:8]:
    print(f"   '{it['text']}' cx={int(it['cx'])} cy={int(it['cy'])} score={it['score']:.2f}")
