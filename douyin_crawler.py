# -*- coding: utf-8 -*-
"""
douyin_crawler.py
================================================================================
抖音商城(livelite)「拍同款」抓取 - adb + RapidOCR 方案
================================================================================
放弃 Appium（livelite 是 Flutter 应用，UiAutomator 读元素树必崩），改用：
  - AdbController : adb 截图/点击/滑动/启动/推图
  - OcrLocator    : RapidOCR 识别文字 + bbox 中心坐标定位
  - TemplateMatcher: OpenCV 模板匹配定位无文字图标（相机/搜索/返回）
  - DouyinCrawler : 流程编排（启动 livelite → 进拍同款 → 选图 → 抓商品 → 滑动）

核心思路：用 OCR 文字 bbox 中心坐标驱动点击，不硬编码坐标。
⚠️ 拍同款入口/选图/结果页布局需真机逐步校准（见各方法注释的「真机调试」）。
"""

import os
import re
import time
import logging
import tempfile
import subprocess

import cv2
import numpy as np
from rapidocr_onnxruntime import RapidOCR

import config
import locators


# ==============================================================================
# AdbController：封装 adb 操作
# ==============================================================================
class AdbController:
    """封装常用 adb 命令：截图、点击、滑动、启动 Activity、推送文件、查前台。"""

    def __init__(self, adb_path, udid):
        self.adb = adb_path
        self.udid = udid

    def _run(self, args, timeout=None):
        """执行 adb 命令，返回 (code, stdout_bytes, stderr_bytes)。"""
        timeout = timeout or config.ADB_CONFIG["cmd_timeout"]
        cmd = [self.adb, "-s", self.udid] + args
        return subprocess.run(cmd, capture_output=True, timeout=timeout)

    def shell(self, cmd_str, timeout=None):
        """执行 `adb shell <cmd_str>`，返回 stdout 字符串。"""
        r = self._run(["shell", cmd_str], timeout)
        return r.stdout.decode("utf-8", errors="ignore")

    def screencap(self, local_path):
        """截图到本地文件。

        用 `adb exec-out screencap -p` 直接拿 PNG 字节流，由 Python 写文件，
        避开 `adb pull` + 中文路径在 Windows 下的编码问题（adb 会把中文路径解析乱）。
        """
        r = subprocess.run(
            [self.adb, "-s", self.udid, "exec-out", "screencap", "-p"],
            capture_output=True,
            timeout=config.ADB_CONFIG["cmd_timeout"],
        )
        if r.returncode != 0 or not r.stdout:
            raise RuntimeError(
                f"screencap 失败(code={r.returncode}): "
                f"{r.stderr.decode('utf-8', errors='ignore')[:200]}"
            )
        with open(local_path, "wb") as f:
            f.write(r.stdout)
        return local_path

    def tap(self, x, y):
        """点击坐标。"""
        self.shell(f"input tap {int(x)} {int(y)}")

    def swipe(self, x1, y1, x2, y2, ms):
        """滑动手势。"""
        self.shell(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(ms)}")

    def swipe_up(self, ratio=0.7):
        """从屏幕 ratio 处上滑到 1-ratio 处（向上翻页）。"""
        ms = config.CRAWL_CONFIG["swipe_duration_ms"]
        size = self.window_size()
        w, h = size["w"], size["h"]
        self.swipe(w * 0.5, h * ratio, w * 0.5, h * (1 - ratio), ms)

    def window_size(self):
        """返回屏幕宽高 {'w','h'}。"""
        out = self.shell("wm size")
        m = re.search(r"(\d+)x(\d+)", out)
        if m:
            return {"w": int(m.group(1)), "h": int(m.group(2))}
        return {"w": 1080, "h": 2400}

    def am_start(self, component):
        """启动指定组件（package/activity 或 package/activity 全称）。"""
        self.shell(f"am start -n {component}")

    def push(self, local, remote_dir):
        """推送文件到设备目录。"""
        self._run(["push", local, remote_dir])

    def media_scan(self, device_file):
        """触发媒体扫描，让相册立即看到新图片。"""
        try:
            self.shell(
                f'am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE '
                f'-d file://{device_file}'
            )
        except Exception:
            pass

    def foreground(self):
        """返回当前前台 app 包名。"""
        out = self.shell("dumpsys activity activities")
        m = re.search(r"topResumedActivity.*?(com\.\w+\.\w+(?:\.\w+)*)/", out)
        return m.group(1) if m else ""

    def back(self):
        """系统返回键。"""
        self.shell("input keyevent 4")


# ==============================================================================
# OcrLocator：RapidOCR 识别 + 关键词定位
# ==============================================================================
class OcrLocator:
    """RapidOCR 识别文字，提供按关键词查找、按价格符号定位等方法。"""

    def __init__(self):
        # 首次加载模型约 2-3 秒
        self.engine = RapidOCR()

    def recognize(self, img_path):
        """识别图片，返回 item 列表 [{text,cx,cy,box,score}]。"""
        result, _ = self.engine(img_path)
        items = []
        if not result:
            return items
        for box, text, score in result:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            items.append({
                "text": text,
                "cx": sum(xs) / 4,
                "cy": sum(ys) / 4,
                "top": min(ys),
                "bottom": max(ys),
                "left": min(xs),
                "right": max(xs),
                "score": float(score),
            })
        return items

    def find_text(self, items, keywords, score_min=0.5):
        """在 items 中找含任一关键词的项，返回第一个匹配（或 None）。"""
        for it in items:
            if it["score"] < score_min:
                continue
            for kw in keywords:
                if kw in it["text"]:
                    return it
        return None

    def find_prices(self, items):
        """返回疑似价格项（含 ¥/￥/$ 且含数字），按从上到下排序。"""
        prices = []
        for it in items:
            t = it["text"]
            if any(sym in t for sym in locators.PRICE_SYMBOLS) and re.search(r"\d", t):
                prices.append(it)
        prices.sort(key=lambda x: x["cy"])
        return prices


# ==============================================================================
# TemplateMatcher：OpenCV 模板匹配定位无文字图标（可选）
# ==============================================================================
class TemplateMatcher:
    """用 OpenCV matchTemplate 在截图中找图标模板，返回匹配中心坐标。"""

    def __init__(self, template_dir, threshold=None):
        self.template_dir = template_dir
        self.threshold = threshold or config.TEMPLATE_CONFIG["match_threshold"]

    def find(self, screenshot_path, template_name):
        """在截图中找模板 template_name，返回 (cx, cy) 或 None。"""
        tpl_path = os.path.join(self.template_dir, template_name)
        if not os.path.exists(tpl_path):
            return None
        scene = cv2.imread(screenshot_path)
        tpl = cv2.imread(tpl_path)
        if scene is None or tpl is None:
            return None
        res = cv2.matchTemplate(scene, tpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val < self.threshold:
            return None
        h, w = tpl.shape[:2]
        return (max_loc[0] + w / 2, max_loc[1] + h / 2)


# ==============================================================================
# DouyinCrawler：流程编排
# ==============================================================================
class DouyinCrawler:
    """抖音商城拍同款抓取主流程。"""

    def __init__(self):
        self.adb = AdbController(config.ADB_CONFIG["adb_path"], config.ADB_CONFIG["udid"])
        self.ocr = OcrLocator()
        self.tmpl = TemplateMatcher(config.TEMPLATE_CONFIG["template_dir"])
        self._shot_counter = 0
        self._setup_logging()

    def _setup_logging(self):
        self.logger = logging.getLogger("DouyinCrawler")
        self.logger.setLevel(logging.INFO)
        # 不向 root logger 传播，避免与 main.py 的 basicConfig 重复输出两行
        self.logger.propagate = False
        if not self.logger.handlers:
            h = logging.StreamHandler()
            h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            self.logger.addHandler(h)

    # ---------- 基础工具 ----------
    def _shot(self):
        """截图到 output/screenshots/shot_N.png，返回路径。"""
        os.makedirs(config.OUTPUT_CONFIG["screenshot_dir"], exist_ok=True)
        path = os.path.join(
            config.OUTPUT_CONFIG["screenshot_dir"], f"shot_{self._shot_counter}.png"
        )
        self._shot_counter += 1
        return self.adb.screencap(path)

    def _shot_ocr(self):
        """截图 + OCR，返回 items。"""
        return self.ocr.recognize(self._shot())

    def _tap_keyword(self, keywords, settle=None, score_min=0.5):
        """截图→OCR 找关键词→点其中心。成功返回 True。"""
        items = self._shot_ocr()
        it = self.ocr.find_text(items, keywords, score_min)
        if not it:
            self.logger.info(f"未找到关键词: {keywords}")
            return False
        self.logger.info(f"点击 '{it['text']}' @({int(it['cx'])},{int(it['cy'])})")
        self.adb.tap(it["cx"], it["cy"])
        time.sleep(settle if settle is not None else config.CRAWL_CONFIG["settle_seconds"])
        return True

    def _wait_text(self, keywords, timeout=15, interval=1.5):
        """等待某关键词出现，出现返回其 item，超时返回 None。"""
        end = time.time() + timeout
        while time.time() < end:
            it = self.ocr.find_text(self._shot_ocr(), keywords)
            if it:
                return it
            time.sleep(interval)
        return None

    # ---------- 流程步骤 ----------
    def start_app(self):
        """启动 livelite 到首页。唤醒屏幕 + force-stop 清残留 task + 重新启动。

        force-stop 是为了清除上次调试残留的 task（否则 am start 可能带到旧界面，
        如拍同款页，而非首页）。
        """
        self.logger.info("启动 livelite ...")
        self.adb.shell("input keyevent 224")  # 唤醒屏幕（防灭屏）
        time.sleep(0.5)
        self.adb.shell(f"am force-stop {config.APP_CONFIG['package']}")  # 清残留 task
        time.sleep(1)
        self.adb.am_start(f"{config.APP_CONFIG['package']}/{config.APP_CONFIG['launch_activity']}")
        time.sleep(config.CRAWL_CONFIG["settle_seconds"] * 2)

    def enter_scan(self):
        """进入拍同款：在 livelite 首页点搜索框右侧的相机图标。

        ScanCommodityActivity 无法从外部 am start（会回桌面），必须走 UI。
        策略：OCR 找顶部「搜索」按钮作锚点 → 相机在其左侧约 90px → 点击 → 验证出现「相册/识别」。
        （已真机验证：搜索按钮 cx≈1289，相机 ≈ left-90 = 1141，点击后进入拍同款页）
        """
        # 等首页加载：「搜索」按钮出现(冷启动首页加载慢，单次 OCR 易漏)
        anchor = self._wait_text(["搜索"], timeout=12)
        if not anchor:
            self.logger.warning("未找到顶部'搜索'按钮，无法定位相机入口")
            return False
        cam_x = anchor["left"] - 90
        cam_y = anchor["cy"]
        self.logger.info(f"点击相机图标 @({int(cam_x)},{int(cam_y)})")
        self.adb.tap(cam_x, cam_y)
        time.sleep(config.CRAWL_CONFIG["settle_seconds"])
        if self._wait_text(["相册", "识别", "对准物品"], timeout=8):
            self.logger.info("已进入拍同款 ✓")
            return True
        self.logger.warning("点击相机后未确认进入拍同款")
        return False

    def push_image(self):
        """把本地待搜索图片推送到手机相册目录。"""
        src = config.CRAWL_CONFIG["search_image_path"]
        if not os.path.isfile(src):
            raise FileNotFoundError(f"待搜索图片不存在: {src}")
        dev_dir = config.APP_CONFIG["device_image_dir"]
        dev_path = dev_dir + os.path.basename(src)
        self.logger.info(f"推送图片: {src} -> {dev_path}")
        self.adb.push(src, dev_dir)
        self.adb.media_scan(dev_path)
        return dev_path

    def upload_image(self):
        """拍同款页：相册 → 选首张图 → 完成。

        真机调试（已部分验证）：
        - 点「相册」用 OCR 定位（已验证 @(1167,2858)）
        - 相册网格首图用估算坐标 w*0.15, h*0.28（左上角，需按实际校准）
        - 「完成」按钮：优先 OCR 找，找不到点右上角 w*0.93, h*0.05
        """
        self.logger.info("进入相册选图 ...")
        self._tap_keyword(locators.ALBUM)
        time.sleep(2)
        size = self.adb.window_size()
        w, h = size["w"], size["h"]
        self.logger.info("选择首张图片(相册第一行第一张)")
        self.adb.tap(w * 0.125, h * 0.22)
        time.sleep(1.5)
        # 完成按钮：优先 OCR 找「完成」，找不到点右上角
        self.logger.info("点完成发起搜索 ...")
        if not self._tap_keyword(["完成", "确定", "下一步"], settle=2, score_min=0.5):
            self.logger.info("OCR 未找到完成按钮，点右上角")
            self.adb.tap(w * 0.90, h * 0.05)
            time.sleep(2)

    def collect_goods(self, seen_titles):
        """识别当前屏的商品，按价格符号关联标题/店铺，返回新增商品列表。

        真机调试：拍同款结果页的卡片布局（标题/价格/店铺相对位置）需据此调整启发式。
        """
        items = self._shot_ocr()
        prices = self.ocr.find_prices(items)
        goods = []
        for price_it in prices:
            # 标题：在价格上方且不重叠的非价格文字（取最近的一条较长文本）
            title = ""
            best = None
            for it in items:
                if it is price_it:
                    continue
                if it["bottom"] <= price_it["top"] + 20 and it["cx"] < price_it["right"] + 200:
                    # 在价格上方区域
                    if len(it["text"]) > len(title):
                        title = it["text"]
                        best = it
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            # 店铺：含店铺关键词的文字，在价格附近
            shop = ""
            for it in items:
                if any(kw in it["text"] for kw in locators.SHOP_KEYWORDS):
                    if abs(it["cy"] - price_it["cy"]) < 300:
                        shop = it["text"]
                        break
            goods.append({
                "title": title,
                "price": price_it["text"],
                "shop": shop,
                "cy": price_it["cy"],
            })
            self.logger.info(f"抓到: {title} | {price_it['text']} | {shop}")
        return goods

    # ---------- 详情页抓取（完整标题 + 完整参数） ----------
    def enter_detail(self, price_item):
        """点击结果页某商品的价格坐标，进入该商品详情页并等待加载。

        price_item: collect_goods 返回的价格项（含 cx/cy）。
        详情页第一屏主要是商品大图，标题/参数在下方，需后续滚动才看得到。
        """
        self.logger.info(
            f"进入详情页：点 '{price_item['text']}' @({int(price_item['cx'])},{int(price_item['cy'])})"
        )
        self.adb.tap(price_item["cx"], price_item["cy"])
        time.sleep(4)  # 转场等待：详情页加载慢，且页面有直播浮窗/按钮，需等稳定避免误点
        # 等详情页加载完成：直到出现价格(详情页加载标志)，最多再等 6 秒
        end = time.time() + 6
        while time.time() < end:
            if self.ocr.find_prices(self._shot_ocr()):
                time.sleep(1.5)  # 价格出现后再等 1.5 秒，确保页面完全稳定
                return
            time.sleep(1)

    def _scroll_until_prices(self, max_scrolls=4):
        """下滑详情页直到出现价格(此时完整标题也在附近)，返回 (当前屏items, prices)。

        详情页第一屏多为商品大图，标题/价格在其下方，需下滑一两次才露出。
        找到价格即停，避免滑过参数容器太远。
        """
        for _ in range(max_scrolls):
            items = self._shot_ocr()
            prices = self.ocr.find_prices(items)
            if prices:
                return items, prices
            self.adb.swipe_up()
            time.sleep(config.CRAWL_CONFIG["settle_seconds"])
        items = self._shot_ocr()
        return items, self.ocr.find_prices(items)

    def find_params_container(self, max_scrolls=None):
        """详情页向下滑动，查找「参数容器」入口标签(产品参数/参数/规格)。

        抖音商城详情页的参数放在一个专门容器里，需下滑才能看到其入口标签，
        标签表面会显示几个参数。找到返回其 OCR item(含坐标)，找不到返回 None。
        """
        max_scrolls = max_scrolls or config.DETAIL_CONFIG["max_scrolls_find_params"]
        for i in range(max_scrolls):
            items = self._shot_ocr()
            f = self.ocr.find_text(items, locators.DETAIL_PARAMS_REGION)
            # 防御：排除"商品详情"等图文详情区块(不是参数网格)
            if f and "详情" not in f["text"]:
                self.logger.info(
                    f"第{i+1}次滑动找到参数容器 '{f['text']}' @({int(f['cx'])},{int(f['cy'])})"
                )
                return f
            self.adb.swipe_up()
            time.sleep(config.CRAWL_CONFIG["settle_seconds"])
        self.logger.warning(f"滑动 {max_scrolls} 次未找到参数容器")
        return None

    def find_and_collect_params(self, max_scrolls=None):
        """下滑详情页查找参数网格并直接配对参数(无需点标签)。

        真机验证：抖音详情页参数常以网格「直接显示」(无"产品参数"标签入口)，
        下滑到即可见(value 上行 + key 下行)。逐屏 OCR + collect_params 配对，返回
        第一个含 ≥2 项参数的结果；找不到返回空 dict。
        """
        max_scrolls = max_scrolls or config.DETAIL_CONFIG["max_scrolls_find_params"]
        for i in range(max_scrolls):
            items = self._shot_ocr()
            params = self.collect_params(items)
            if len(params) >= 2:
                # OCR 有随机性，再识别一次合并：同 key 取较长的 value(减少"1920*1080→192"截断)
                params2 = self.collect_params(self._shot_ocr())
                for k, v in params2.items():
                    if k not in params or len(v) > len(params[k]):
                        params[k] = v
                self.logger.info(f"第{i+1}屏找到参数网格，配对 {len(params)} 项")
                return params
            self.adb.swipe_up()
            time.sleep(config.CRAWL_CONFIG["settle_seconds"])
        self.logger.warning(f"滑动 {max_scrolls} 次未找到参数网格")
        return {}

    def find_params_entry(self, max_scrolls=None):
        """下滑详情页找「退货包邮券·7天无理由退货」锚点，返回其 OCR item。

        参数容器(表面参数卡片)紧贴在该锚点正上方，点击卡片可进入完整参数页。
        用完整锚点「7天无理由」定位，避免「退货包邮券」部分匹配到上方优惠券条。
        """
        max_scrolls = max_scrolls or config.DETAIL_CONFIG["max_scrolls_find_params"]
        for i in range(max_scrolls):
            items = self._shot_ocr()
            anchor = self.ocr.find_text(items, ["7天无理由", "无理由退货"])
            if anchor:
                self.logger.info(f"第{i+1}屏找到参数锚点 '{anchor['text']}'")
                return anchor
            self.adb.swipe_up()
            time.sleep(config.CRAWL_CONFIG["settle_seconds"])
        self.logger.warning(f"滑动 {max_scrolls} 次未找到参数锚点")
        return None

    def _is_param_key(self, it):
        """单项是否像参数 key(含参数名，排除卖点/操作/标签/容器标题)。"""
        t = it["text"]
        if any(kw in t for kw in locators.PARAM_SELLER_WORDS):
            return False
        if any(kw in t for kw in ["客服", "购物车", "下单", "产品参数", "商品参数",
                                    "查看", "领取", "进入", "关闭", "更多", "详情"]):
            return False
        return any(kw in t for kw in locators.PARAM_KEY_HINTS)

    def collect_params_full(self, items):
        """完整参数页键值对配对：每个 key(含参数名) 找最近 value。

        完整参数页布局：多数参数「key 左 + value 右」同行；前几行网格「value 上 +
        key 下」。对每个 key 优先配同行右侧(cx 更大)的非 key 项，其次配正上方。
        每个 value 只用一次(used 去重)，避免多 key 抢同一 value。
        """
        keys = [it for it in items if self._is_param_key(it)]
        params = {}
        used = set()
        for k in keys:
            cands = []
            for v in items:
                if v is k or id(v) in used:
                    continue
                if self._is_param_key(v):
                    continue
                dcy = v["cy"] - k["cy"]
                dcx = v["cx"] - k["cx"]
                if abs(dcy) < 45 and 50 < dcx < 800:        # 同行右侧(key左 value右)
                    cands.append((v, dcx))
                elif -130 < dcy < -30 and abs(dcx) < 260:    # 正上方(value上 key下)
                    cands.append((v, 100000 + abs(dcx)))
            if cands:
                cands.sort(key=lambda x: x[1])
                params[k["text"].strip()] = cands[0][0]["text"].strip()
                used.add(id(cands[0][0]))
        return params

    def collect_full_params_page(self):
        """进入完整参数页(点参数容器)，上滑收集全部参数，返回键值对 dict。

        完整参数页参数较多，一屏放不下，需上滑多次收集；按 key 去重合并。
        流程：找锚点 → 点参数容器进完整页 → 逐屏 OCR+配对+上滑 → 关闭完整页。
        """
        anchor = self.find_params_entry()
        if not anchor:
            return {}
        w = self.adb.window_size()["w"]
        # 参数容器(表面参数)紧贴锚点正上方约 212px
        self.logger.info(f"点击参数容器进完整参数页 @({int(w*0.42)},{int(anchor['cy']-212)})")
        self.adb.tap(w * 0.42, anchor["cy"] - 212)
        time.sleep(config.DETAIL_CONFIG["detail_settle_seconds"])
        params = {}
        for j in range(6):
            batch = self.collect_params_full(self._shot_ocr())
            new = {k: v for k, v in batch.items() if k not in params}
            params.update(batch)
            self.logger.info(f"完整参数页第{j+1}屏 +{len(new)} 项(累计{len(params)})")
            if not new and j > 0:
                break
            self.adb.swipe_up()
            time.sleep(config.CRAWL_CONFIG["settle_seconds"])
        self.adb.back()  # 关闭完整参数页
        time.sleep(config.CRAWL_CONFIG["settle_seconds"])
        return params

    def _looks_like_key_row(self, items):
        """判断一行文本项是否像「参数 key 行」(参数名)。

        参数名含已知提示词(locators.PARAM_KEY_HINTS: 是否/率/类型/分辨率/刷新/面板
        等)。严格要求含提示词——去掉「中文占比>0.5」兜底，因为它会把底部操作栏
        (客服/购物车/现在下单/旗舰店 等纯中文行)误判为 key 行；并排除含操作按钮词
        的行，双保险。
        """
        text = "".join(it["text"] for it in items)
        if not text:
            return False
        # 排除底部操作栏/按钮行
        if any(kw in text for kw in ["客服", "购物车", "下单", "领取", "查看",
                                      "进入", "去换", "立即", "加入", "去购", "好评率"]):
            return False
        # 排除卖点/营销文案行(含参数词根但不是参数，如"广色域/不闪屏/低蓝光")
        if any(kw in text for kw in locators.PARAM_SELLER_WORDS):
            return False
        return any(kw in text for kw in locators.PARAM_KEY_HINTS)

    def _looks_like_value_row(self, items):
        """判断一行文本项是否像「参数 value 行」(纯值，不含参数名)。

        value 多为数字/英文/短中文(否/是/有/无)，不含参数名提示词。
        用于排除「相邻两个 key 行」被误当成 value+key 双行对配对。
        """
        text = "".join(it["text"] for it in items)
        if not text:
            return False
        return not any(kw in text for kw in locators.PARAM_KEY_HINTS)

    def collect_params(self, items):
        """从详情页 OCR items 配对出参数 dict {key: value}。

        抖音参数网格布局：每个单元格「value(上行) + key(下行)」，value 在 key 上方
        约 80px，同一单元格 key/value 的 cx 接近。算法：
          1. 按 cy 聚类成文本行；
          2. 寻找相邻「value 行(上 A) + key 行(下 B)」双行对(cy 差在区间内、两行均
             含≥N 项、B 行像参数名)，B 行每项按 cx 最近配对 A 行的 value。
        """
        cfg = config.DETAIL_CONFIG
        dy_min, dy_max = cfg["param_row_dy"]
        # 排除参数容器标签本身(产品参数/商品参数/规格参数等)——它是容器标题不是
        # 参数项，不排除会出现"产品参数→退货包邮卷"这类错配
        items = [it for it in items
                 if not any(kw in it["text"] for kw in locators.DETAIL_PARAMS_REGION)]
        # 1. 按 cy 聚类成行
        rows = []
        for it in sorted(items, key=lambda x: x["cy"]):
            placed = False
            for r in rows:
                if abs(it["cy"] - r["cy"]) < cfg["param_row_cy_tol"]:
                    r["items"].append(it)
                    placed = True
                    break
            if not placed:
                rows.append({"cy": it["cy"], "items": [it]})
        rows.sort(key=lambda r: r["cy"])
        # 2. 找「value 行(上) + key 行(下)」双行对配对
        params = {}
        for i in range(len(rows) - 1):
            A, B = rows[i], rows[i + 1]          # A=上行(value), B=下行(key)
            dy = B["cy"] - A["cy"]
            if not (dy_min <= dy <= dy_max):
                continue
            if len(A["items"]) < cfg["param_min_items_per_row"]:
                continue
            if len(B["items"]) < cfg["param_min_items_per_row"]:
                continue
            if not self._looks_like_key_row(B["items"]):
                continue
            # A 行(value 行)应像纯值：不含参数名提示词(否则可能是相邻的两个 key 行误配)
            if not self._looks_like_value_row(A["items"]):
                continue
            for k in B["items"]:
                best, best_d = None, cfg["param_cx_tol"] + 1
                for v in A["items"]:
                    d = abs(k["cx"] - v["cx"])
                    if d < best_d:
                        best_d, best = d, v
                if best:
                    params[k["text"].strip()] = best["text"].strip()
        # 后处理：补全被 OCR 截断的 key(如"分辨"→"分辨率"、"刷新"→"刷新率")
        completed = {}
        for k, v in params.items():
            full = k
            for hint in locators.PARAM_KEY_HINTS:
                if hint.startswith(k) and len(hint) > len(full):
                    full = hint
                    break
            completed[full] = v
        return completed

    def collect_detail_title(self, items, price_cy):
        """从详情页 OCR items 提取完整商品标题。

        列表页标题常被截断，完整标题在详情页「价格下方」一段。取价格下方区间内、
        排除销量/贴息等噪音后的较长文字行，合并相邻行(cy 差小于阈值)为完整标题。
        """
        cfg = config.DETAIL_CONFIG
        dy_lo, dy_hi = cfg["title_search_dy"]
        lo, hi = price_cy + dy_lo, price_cy + dy_hi
        cands = [
            it for it in items
            if lo < it["cy"] < hi
            and len(it["text"]) >= cfg["title_min_chars"]
            and not any(kw in it["text"] for kw in locators.TITLE_NOISE_KEYWORDS)
        ]
        if not cands:
            return ""
        cands.sort(key=lambda x: x["cy"])
        # 合并相邻行(cy 差小于阈值)为同一标题的多行
        groups = [[cands[0]]]
        for it in cands[1:]:
            if it["cy"] - groups[-1][-1]["cy"] < cfg["title_merge_dy"]:
                groups[-1].append(it)
            else:
                groups.append([it])
        best = max(groups, key=lambda g: sum(len(x["text"]) for x in g))
        return "".join(x["text"] for x in best).strip()

    def collect_detail(self, price_item):
        """进入详情页，抓取：完整标题 + 价格(原价/券后) + 店铺 + 完整参数。

        进详情首屏抓标题/价格/店铺，再下滑找参数容器抓参数：
          1. 进详情(不下滑) → 第0屏含 完整标题 + 原价/券后价 + 店铺(诊断证实)；
          2. 下滑找参数容器入口标签 → 点开 → OCR 配对参数；
          3. 返回列表页。

        ⚠️ 标题/价格/店铺必须在「第0屏」抓：若先下滑找参数容器，标题会滚出屏顶
        (曾因此把卖点摘要当成标题)。参数入口在第0屏之下，需下滑才出现。
        """
        self.enter_detail(price_item)
        # 第0屏(进详情不下滑)：完整标题 / 原价 / 券后价 / 店铺 都在此屏
        screen = self._shot_ocr()
        prices = self.ocr.find_prices(screen)
        price_cy = prices[0]["cy"] if prices else 2400
        title = self.collect_detail_title(screen, price_cy)
        origin_price = prices[0]["text"] if prices else price_item["text"]
        coupon_price = ""
        for p in prices:
            if "券后" in p["text"] or "卷后" in p["text"]:
                coupon_price = p["text"]
                break
        shop = ""
        # 店铺：按优先级(旗舰 > 官方 > 自营/专营/专卖)找，排除"店铺销量"等文案
        for kw in ["旗舰", "官方", "自营", "专营", "专卖"]:
            for it in screen:
                if kw in it["text"] and "销量" not in it["text"]:
                    shop = it["text"]
                    break
            if shop:
                break
        # 进完整参数页(点参数容器)，上滑收集全部参数(键值对)
        params = self.collect_full_params_page()
        self.logger.info(
            f"详情抓取：标题='{title[:30]}' 原价={origin_price} 券后={coupon_price} "
            f"店铺={shop} 参数{len(params)}项"
        )
        # 返回列表页
        self.adb.back()
        time.sleep(config.CRAWL_CONFIG["settle_seconds"])
        return {
            "title": title,
            "price": origin_price,
            "coupon_price": coupon_price,
            "shop": shop,
            "params": params,
        }

    def scroll_and_collect(self):
        """滑动加载 + 抓取，去重，达上限停止。"""
        seen = set()
        all_goods = []
        limit = config.CRAWL_CONFIG["results_limit"]
        for i in range(config.CRAWL_CONFIG["max_scroll_times"]):
            self.logger.info(f"第 {i+1} 轮抓取 ...")
            batch = self.collect_goods(seen)
            all_goods.extend(batch)
            if len(all_goods) >= limit:
                all_goods = all_goods[:limit]
                break
            before = len(seen)
            self.adb.swipe_up()
            time.sleep(config.CRAWL_CONFIG["settle_seconds"])
            if len(seen) == before and i > 0:
                self.logger.info("无新商品，停止滑动")
                break
        return all_goods

    def run(self):
        """执行完整流程，返回商品列表。"""
        self.start_app()
        if not self.enter_scan():
            return []
        self.push_image()
        self.upload_image()
        # 等待结果加载
        self.logger.info("等待拍同款结果 ...")
        self._wait_text(["已找到", "相似", "同款", "商品"] + locators.PRICE_SYMBOLS, timeout=20)
        return self.scroll_and_collect()

    def run_detail(self, max_goods=5):
        """到列表，逐个进详情抓取完整信息(标题/价格/店铺/参数)。

        每次重新 OCR 列表当前屏，按「未抓过的价格」选下一个商品进详情——避免详情
        返回后列表重绘导致预存坐标失效、或重复进同一商品。返回详情 dict 列表。
        """
        self.start_app()
        if not self.enter_scan():
            return []
        self.push_image()
        self.upload_image()
        self.logger.info("等待拍同款结果 ...")
        self._wait_text(["已找到", "相似", "同款", "商品"] + locators.PRICE_SYMBOLS, timeout=20)
        results = []
        seen_prices = set()   # 已抓过的价格文本(去重，避免重复进同一商品)
        idle_scrolls = 0      # 连续未抓到新商品的下滑次数(防死循环)
        w = self.adb.window_size()["w"]
        while len(results) < max_goods and idle_scrolls < 4:
            items = self._shot_ocr()
            all_prices = self.ocr.find_prices(items)
            # 只点左半屏商品：直播入口在卡片右下角，点右半易进直播间而非商品详情
            prices = [p for p in all_prices if p["cx"] < w * 0.5]
            p = next((x for x in prices if x["text"] not in seen_prices), None)
            self.logger.info(
                f"列表扫描: 识别价格{len(all_prices)}个(左半屏{len(prices)}个) "
                f"{[x['text'] + '@cx' + str(int(x['cx'])) for x in all_prices]} | "
                f"已抓{seen_prices} | {'→点击' + p['text'] if p else '→下滑'}"
            )
            if not p:
                # 当前屏无新商品，下滑加载更多
                self.adb.swipe_up()
                time.sleep(config.CRAWL_CONFIG["settle_seconds"])
                idle_scrolls += 1
                continue
            seen_prices.add(p["text"])
            self.logger.info(f"=== 第 {len(results)+1}/{max_goods} 个商品: {p['text']} ===")
            try:
                results.append(self.collect_detail(p))
                idle_scrolls = 0  # 抓到新商品，重置空闲计数
            except Exception as e:
                self.logger.warning(f"商品详情抓取失败: {e}")
                self.adb.back()  # 兜底返回列表
                time.sleep(config.CRAWL_CONFIG["settle_seconds"])
        return results
