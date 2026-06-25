# -*- coding: utf-8 -*-
"""
main.py
================================================================================
抖音商城(livelite)「拍同款」抓取 - 运行入口（adb + RapidOCR 方案）
================================================================================
职责：解析参数 → 调用 DouyinCrawler.run() → 保存 JSON/TXT 结果。
不需要 appium server，纯 adb + Python + OCR。

运行：
    uv run python main.py
    uv run python main.py --image "D:\\xxx\\鞋.jpg"   # 临时覆盖待搜索图片
"""

import os
import sys
import json
import argparse
import logging

import config
from douyin_crawler import DouyinCrawler


def parse_args():
    """解析命令行参数：支持临时覆盖待搜索的本地图片路径。"""
    parser = argparse.ArgumentParser(
        description="抖音商城「拍同款」抓取 (adb + RapidOCR)"
    )
    parser.add_argument(
        "--image",
        default=None,
        help="待搜索的本地图片绝对路径（覆盖 config.CRAWL_CONFIG.search_image_path）",
    )
    parser.add_argument(
        "--detail",
        action="store_true",
        help="进商品详情页抓取完整标题 + 完整参数(默认只抓列表标题/价格/店铺)",
    )
    parser.add_argument(
        "--max-goods",
        type=int,
        default=5,
        help="--detail 模式下抓取的商品数量(列表第一屏前 N 个，默认 5)",
    )
    return parser.parse_args()


def ensure_output_dir():
    """确保结果输出目录存在。"""
    os.makedirs(config.OUTPUT_CONFIG["output_dir"], exist_ok=True)


def save_results(goods):
    """将抓取结果保存为 JSON + 人类可读 txt 摘要。"""
    ensure_output_dir()
    json_path = os.path.join(
        config.OUTPUT_CONFIG["output_dir"], config.OUTPUT_CONFIG["output_file"]
    )
    # 序列化时去掉调试用的 bbox 字段，避免 JSON 臃肿
    clean = [{k: v for k, v in g.items() if k != "box"} for g in goods]
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    print(f"\n[保存] JSON: {json_path}（共 {len(goods)} 件商品）")

    if config.OUTPUT_CONFIG["save_txt_summary"]:
        txt_path = os.path.join(
            config.OUTPUT_CONFIG["output_dir"], config.OUTPUT_CONFIG["txt_file"]
        )
        with open(txt_path, "w", encoding="utf-8") as f:
            for idx, g in enumerate(goods, 1):
                f.write(f"#{idx} {g.get('title', '')}\n")
                price_line = g.get('price', '')
                if g.get('coupon_price'):
                    price_line += f"（{g['coupon_price']}）"
                f.write(f"   价格: {price_line}\n")
                f.write(f"   店铺: {g.get('shop', '')}\n")
                params = g.get('params')
                if params:
                    f.write("   参数:\n")
                    for k, v in params.items():
                        f.write(f"     {k}: {v}\n")
                f.write("\n")
        print(f"[保存] TXT: {txt_path}")


def save_excel(goods):
    """保存到 Excel：每商品一行，参数合并到「参数」列(换行显示 key:value)。"""
    import openpyxl
    from openpyxl.styles import Alignment, Font

    ensure_output_dir()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "抖音拍同款"
    ws.append(["序号", "标题", "原价", "券后价", "店铺", "参数"])
    for c in ws[1]:
        c.font = Font(bold=True)
    for idx, g in enumerate(goods, 1):
        params = g.get("params") or {}
        param_text = "\n".join(f"{k}: {v}" for k, v in params.items())
        ws.append([idx, g.get("title", ""), g.get("price", ""),
                   g.get("coupon_price", ""), g.get("shop", ""), param_text])
    # 参数列自动换行 + 设置列宽
    for row in ws.iter_rows(min_row=2, min_col=6, max_col=6):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    for col, width in zip("ABCDEF", [6, 42, 12, 14, 16, 52]):
        ws.column_dimensions[col].width = width
    path = os.path.join(config.OUTPUT_CONFIG["output_dir"], config.OUTPUT_CONFIG["excel_file"])
    try:
        wb.save(path)
    except PermissionError:
        # 文件被占用(Excel 打开着)，改用带时间戳的备选文件名，避免整个程序崩
        import time as _t
        path = os.path.join(config.OUTPUT_CONFIG["output_dir"],
                            f"douyin_results_{_t.strftime('%Y%m%d_%H%M%S')}.xlsx")
        wb.save(path)
        print("[警告] douyin_results.xlsx 被占用(请关闭 Excel)，已存到带时间戳的备选文件")
    print(f"[保存] Excel: {path}")


def main():
    """主流程：解析参数 → 运行抓取 → 保存结果。"""
    args = parse_args()
    if args.image:
        config.CRAWL_CONFIG["search_image_path"] = os.path.abspath(args.image)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    print("=" * 70)
    print(" 抖音商城「拍同款」抓取（adb + RapidOCR）")
    print("=" * 70)

    crawler = DouyinCrawler()
    try:
        goods = crawler.run_detail(args.max_goods) if args.detail else crawler.run()
    except FileNotFoundError as e:
        print(f"\n[错误] 文件不存在: {e}")
        sys.exit(2)
    except Exception as e:  # 兜底：保证异常可见而非静默吞掉
        logging.exception("抓取过程中发生未预期异常")
        sys.exit(1)

    if not goods:
        print("\n[警告] 未抓取到商品，请检查 locators 关键词或拍同款流程是否走通")
        sys.exit(0)

    save_results(goods)
    save_excel(goods)
    print("\n抓取完成 ✅")


if __name__ == "__main__":
    main()
