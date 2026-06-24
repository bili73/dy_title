# -*- coding: utf-8 -*-
"""端到端验证 collect_detail：到列表 → 进详情 → 抓标题/价格/店铺/参数。"""
import os, sys, json
sys.path.insert(0, r"D:\桌面\抖音数据抓取(非影刀)")
os.chdir(r"D:\桌面\抖音数据抓取(非影刀)")
from douyin_crawler import DouyinCrawler

c = DouyinCrawler()
c.start_app(); c.enter_scan(); c.push_image(); c.upload_image()
c._wait_text(["相似","同款","已找到","¥","￥"], timeout=20)
items = c._shot_ocr()
prices = c.ocr.find_prices(items)
if not prices:
    print("列表无价格"); sys.exit(0)
p = prices[0]
print(f"\n=== 对列表第1个商品 '{p['text']}' 进详情抓取 ===")
detail = c.collect_detail(p)
print("\n=== collect_detail 结果 ===")
print(json.dumps(detail, ensure_ascii=False, indent=2))
