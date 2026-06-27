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
import threading
import subprocess

import cv2
import numpy as np
from rapidocr_onnxruntime import RapidOCR

import config
import locators


class CrawlerStopped(Exception):
    """用户终止抓取时由 _check_stop 抛出；run_detail/scroll_and_collect 捕获后返回已抓的部分结果。"""


# ==============================================================================
# AdbController：封装 adb 操作
# ==============================================================================
class AdbController:
    """封装常用 adb 命令：截图、点击、滑动、启动 Activity、推送文件、查前台。"""

    def __init__(self, adb_path, udid):
        self.adb = adb_path
        self.logger = logging.getLogger("AdbController")
        self._dpi = None  # 懒加载缓存：首次 dpi() 调用时解析 wm density
        # udid 为 "auto"/None 时自动识别在线设备；否则用传入的写死序列号
        self.udid = self._resolve_udid() if udid in (None, "auto") else udid

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

    def _resolve_udid(self):
        """自动识别在线设备序列号：`adb devices` 取第一个 state=device 的设备。

        config.ADB_CONFIG["udid"]="auto" 时调用。裸跑 `adb devices`（此时还没 udid，
        不能带 -s，故不经 _run）。0 个在线设备抛错；多设备取第一个并告警，提示在
        config 写死具体序列号。
        """
        r = subprocess.run([self.adb, "devices"], capture_output=True,
                           timeout=config.ADB_CONFIG["cmd_timeout"])
        out = r.stdout.decode("utf-8", errors="ignore")
        devs = []
        for line in out.splitlines():
            m = re.match(r"^(?P<sn>\S+)\s+(?P<state>device|offline|unauthorized)", line)
            if m and m.group("state") == "device":
                devs.append(m.group("sn"))
        if not devs:
            raise RuntimeError("adb devices 无在线设备，请检查 USB 连接/调试/授权")
        if len(devs) > 1:
            self.logger.warning(
                f"检测到多个设备 {devs}，使用第一个 '{devs[0]}'；"
                f"如需指定请在 config.ADB_CONFIG['udid'] 写死序列号"
            )
        return devs[0]

    def dpi(self):
        """返回设备 dpi（px/inch），懒加载缓存。

        解析 `adb shell wm density`，Override density 优先（用户改过的实际生效值），
        否则取 Physical density。解析失败回退 480（= 基准设备值，保证读不到 dpi 时
        行为不退化）并告警，不静默。
        """
        if self._dpi is not None:
            return self._dpi
        out = self.shell("wm density")
        m = (re.search(r"Override\s+density:?\s*(\d+)", out)
             or re.search(r"Physical\s+density:?\s*(\d+)", out))
        if m:
            self._dpi = int(m.group(1))
        else:
            self._dpi = 480
            self.logger.warning("无法读取 wm density，按 480dpi 换算 dp，请确认设备连接")
        return self._dpi

    def dp(self, n):
        """dp（density-independent pixel）转 px：px = round(n * dpi / 160)。

        Android 标准 dp 定义。所有坐标偏移/配对容差用 dp 表达，调用本方法按设备实际
        dpi 换算，实现跨分辨率/DPI 适配。基准 480dpi 下 1dp=3px。
        """
        return round(n * self.dpi() / 160.0)

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
    """OCR 识别文字 + bbox 中心坐标定位。

    走 PaddleOCR HTTP 服务(docker, POST /ocr multipart file)，返回 {lines:[{text,box,score}]}。
    box 为 4 角点，据此算 cx/cy/top/bottom/left/right，供点击/参数配对使用。
    """

    def __init__(self):
        import requests  # 仅此处用，局部导入避免顶部强依赖
        self._requests = requests
        self.url = config.OCR_CONFIG["paddleocr_url"]
        self.timeout = config.OCR_CONFIG["timeout"]

    def recognize(self, img_path):
        """识别图片，返回 item 列表 [{text,cx,cy,top,bottom,left,right,score}]。

        ⚠️ 依赖服务返回 box；若服务只返回文字(lines 为字符串)则坐标无法获取，会跳过。
        PaddleOCR CPU 推理较慢，偶发超时，故重试最多 3 次。
        """
        import time
        last_err = None
        data = None
        for attempt in range(3):
            try:
                with open(img_path, "rb") as f:
                    resp = self._requests.post(self.url, files={"file": f}, timeout=self.timeout)
                data = resp.json()
                last_err = None
                break
            except Exception as e:
                last_err = e
                if attempt < 2:
                    time.sleep(1)  # 超时/出错稍等再重试
        if last_err is not None or data is None:
            raise RuntimeError(f"OCR 服务请求失败(重试3次): {last_err}")
        items = []
        for ln in data.get("lines", []):
            # 兼容：lines 元素可能是 dict(A格式带box) 或 str(旧格式无box)
            if isinstance(ln, str):
                continue  # 无坐标，跳过(点击/配对依赖坐标)
            box = ln.get("box")
            if not box or len(box) < 4:
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            items.append({
                "text": ln.get("text", ""),
                "cx": sum(xs) / 4,
                "cy": sum(ys) / 4,
                "top": min(ys),
                "bottom": max(ys),
                "left": min(xs),
                "right": max(xs),
                "score": float(ln.get("score", 1.0)),
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
        self.logger = logging.getLogger("TemplateMatcher")

    @staticmethod
    def _imread(path):
        """读取图片(支持中文路径)。

        cv2.imread 在 Windows 不支持中文路径(返回 None)，用 np.fromfile + cv2.imdecode。
        """
        return cv2.imdecode(np.fromfile(path, dtype=np.uint8), -1)

    def find(self, screenshot_path, template_name):
        """在截图中找模板 template_name，返回 (cx, cy) 或 None。

        多尺度匹配：模板按屏宽比例主缩放 + ±10% 窄范围兜底，适配不同分辨率/DPI 设备
        （模板按 1080 宽裁剪，换机后图标物理大小随屏宽变化）。缩模板不缩 scene；scene
        的 Canny 边缘只算一次，每个尺度仅重算模板边缘 + matchTemplate，取全局最高分。
        """
        tpl_path = os.path.join(self.template_dir, template_name)
        if not os.path.exists(tpl_path):
            self.logger.warning(f"模板不存在: {tpl_path}")
            return None
        scene = self._imread(screenshot_path)
        tpl = self._imread(tpl_path)
        if scene is None or tpl is None:
            self.logger.warning(f"图片读取失败: scene={screenshot_path} tpl={tpl_path}")
            return None
        # 统一为 3 通道 BGR(模板/截图可能是 4 通道 BGRA，matchTemplate 要求通道一致)
        if scene.ndim == 3 and scene.shape[2] == 4:
            scene = cv2.cvtColor(scene, cv2.COLOR_BGRA2BGR)
        if tpl.ndim == 3 and tpl.shape[2] == 4:
            tpl = cv2.cvtColor(tpl, cv2.COLOR_BGRA2BGR)
        # 多尺度：按屏宽算主缩放因子(模板按 1080 宽裁)，±10% 兜底覆盖裁剪/渲染误差
        scale_base = scene.shape[1] / 1080.0
        scales = [scale_base * f for f in (0.9, 1.0, 1.1)]
        scene_edge = cv2.Canny(cv2.cvtColor(scene, cv2.COLOR_BGR2GRAY), 50, 150)
        best_val, best_loc, best_hw = -1.0, None, tpl.shape[:2]
        for s in scales:
            if s <= 0:
                continue
            t = cv2.resize(tpl, None, fx=s, fy=s)  # 缩模板不缩 scene
            # 模板比 scene 还大则跳过(matchTemplate 要求模板 ≤ scene，否则报错)
            if t.shape[0] > scene.shape[0] or t.shape[1] > scene.shape[1]:
                continue
            tpl_edge = cv2.Canny(cv2.cvtColor(t, cv2.COLOR_BGR2GRAY), 50, 150)
            res = cv2.matchTemplate(scene_edge, tpl_edge, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val > best_val:
                best_val, best_loc, best_hw = max_val, max_loc, t.shape[:2]
        if best_loc is None or best_val < self.threshold:
            self.logger.info(
                f"匹配 {template_name} 最高相似度 {best_val:.3f} < 阈值 {self.threshold}"
            )
            return None
        h, w = best_hw
        return (best_loc[0] + w / 2, best_loc[1] + h / 2)

    def find_all(self, screenshot_path, template_name, topn=4):
        """返回多个匹配位置 [(cx, cy, score)]，按相似度降序，NMS 去邻近重复。

        详情页参数入口图标与下部推荐区图标同款，最高峰可能误匹配推荐区。取 topn 个峰，
        配合点击后验证，逐个试到真正进完整参数页的那个。
        """
        tpl_path = os.path.join(self.template_dir, template_name)
        if not os.path.exists(tpl_path):
            return []
        scene = self._imread(screenshot_path)
        tpl = self._imread(tpl_path)
        if scene is None or tpl is None:
            return []
        if scene.ndim == 3 and scene.shape[2] == 4:
            scene = cv2.cvtColor(scene, cv2.COLOR_BGRA2BGR)
        if tpl.ndim == 3 and tpl.shape[2] == 4:
            tpl = cv2.cvtColor(tpl, cv2.COLOR_BGRA2BGR)
        # 主尺度(按屏宽)做 matchTemplate，取 topn 峰值
        scale = scene.shape[1] / 1080.0
        t = cv2.resize(tpl, None, fx=scale, fy=scale)
        if t.shape[0] > scene.shape[0] or t.shape[1] > scene.shape[1]:
            return []
        scene_edge = cv2.Canny(cv2.cvtColor(scene, cv2.COLOR_BGR2GRAY), 50, 150)
        tpl_edge = cv2.Canny(cv2.cvtColor(t, cv2.COLOR_BGR2GRAY), 50, 150)
        res = cv2.matchTemplate(scene_edge, tpl_edge, cv2.TM_CCOEFF_NORMED)
        h, w = t.shape[:2]
        out = []
        res_copy = res.copy()
        for _ in range(topn):
            _, max_val, _, max_loc = cv2.minMaxLoc(res_copy)
            if max_val < self.threshold:
                break
            out.append((max_loc[0] + w / 2, max_loc[1] + h / 2, float(max_val)))
            # NMS：掩盖该峰附近，避免重复取同一目标
            x0 = max(0, max_loc[0] - w)
            x1 = min(res_copy.shape[1], max_loc[0] + 2 * w)
            y0 = max(0, max_loc[1] - h)
            y1 = min(res_copy.shape[0], max_loc[1] + 2 * h)
            res_copy[y0:y1, x0:x1] = 0
        return out


# ==============================================================================
# DouyinCrawler：流程编排
# ==============================================================================
class DouyinCrawler:
    """抖音商城拍同款抓取主流程。"""

    def __init__(self, search_image_path=None, params_keywords=None):
        self.adb = AdbController(config.ADB_CONFIG["adb_path"], config.ADB_CONFIG["udid"])
        self.ocr = OcrLocator()
        self.tmpl = TemplateMatcher(config.TEMPLATE_CONFIG["template_dir"])
        self._shot_counter = 0
        # 待搜索图片路径：传入则用传入(前端覆盖)，否则用 config 默认
        self.search_image_path = search_image_path or config.CRAWL_CONFIG["search_image_path"]
        # 参数容器关键词(前端/CLI 传入)：详情页参数入口卡片上的参数键摘要词
        # (如 "维修方式,上市时间")。OCR 命中即定位到参数摘要行 → 点该行进完整参数页，
        # 替代纯图标模板匹配(齿轮/列表/表盘等图标样式通吃)。为空则回退 param_icon*.png 图标模板。
        self.params_keywords = [k.strip() for k in (params_keywords or []) if k and k.strip()]
        self._setup_logging()
        # 把 config 里的 dp 阈值按设备 dpi 换算成 px 缓存（dpi 仅在此解析一次）
        self._cache_dp_thresholds()
        # 停止/暂停控制（前端按钮通过 server 调 stop/pause/resume）
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # 初始非暂停（set = 允许继续）

    def _setup_logging(self):
        self.logger = logging.getLogger("DouyinCrawler")
        self.logger.setLevel(logging.INFO)
        # 不向 root logger 传播，避免与 main.py 的 basicConfig 重复输出两行
        self.logger.propagate = False
        if not self.logger.handlers:
            h = logging.StreamHandler()
            h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            self.logger.addHandler(h)

    def _cache_dp_thresholds(self):
        """把 config 里的 dp 阈值按设备 dpi 换算成 px，缓存到 self._px。

        dp 值集中定义在 config（DETAIL_CONFIG 的坐标字段 + LIST_OFFSETS_DP），运行时
        一次性换算成 px 存到 self._px。后续 collect_params / collect_detail_title /
        collect_goods / enter_scan 直接读 self._px，零运行时 adb 调用（dpi 仅在此解析
        一次）。与坐标无关的阈值（param_min_items_per_row / title_min_chars）不缓存，
        仍直接读 config。
        """
        dp = self.adb.dp
        cfg = config.DETAIL_CONFIG
        off = config.LIST_OFFSETS_DP
        self._px = {
            "param_row_dy": (dp(cfg["param_row_dy"][0]), dp(cfg["param_row_dy"][1])),
            "param_row_cy_tol": dp(cfg["param_row_cy_tol"]),
            "param_cx_tol": dp(cfg["param_cx_tol"]),
            "title_search_dy": (dp(cfg["title_search_dy"][0]), dp(cfg["title_search_dy"][1])),
            "title_merge_dy": dp(cfg["title_merge_dy"]),
            "camera_left": dp(off["camera_left"]),
            "title_above_dy": dp(off["title_above_dy"]),
            "title_right_dx": dp(off["title_right_dx"]),
            "shop_cy_tol": dp(off["shop_cy_tol"]),
            # 完整参数页 key-value 配对容差
            "full_kv_same_row_dy": dp(cfg["full_kv_same_row_dy"]),
            "full_kv_same_row_dx": (dp(cfg["full_kv_same_row_dx"][0]), dp(cfg["full_kv_same_row_dx"][1])),
            "full_kv_above_dy": (dp(cfg["full_kv_above_dy"][0]), dp(cfg["full_kv_above_dy"][1])),
            "full_kv_above_dx": dp(cfg["full_kv_above_dx"]),
        }
        self.logger.info(
            f"设备 dpi={self.adb.dpi()} → 坐标阈值已按 dp 换算 px: {self._px}"
        )

    # ---------- 停止 / 暂停控制（前端按钮经 server 调用）----------
    def _check_stop(self):
        """循环检查点调用：被要求停止则抛 CrawlerStopped；暂停则阻塞至恢复。

        暂停期间用 wait(0.5) 轮询 stop，保证暂停状态下点终止也能响应。
        """
        if self._stop_event.is_set():
            raise CrawlerStopped()
        while not self._pause_event.is_set():
            if self._stop_event.is_set():
                raise CrawlerStopped()
            self._pause_event.wait(timeout=0.5)

    def stop(self):
        """请求终止：置停止标志，并解除暂停(让阻塞的 _check_stop 退出后抛异常)。"""
        self._stop_event.set()
        self._pause_event.set()

    def pause(self):
        """请求暂停：清 pause 标志，_check_stop 将在下次检查点阻塞。"""
        self._pause_event.clear()

    def resume(self):
        """恢复：置 pause 标志，_check_stop 的阻塞 wait 解除。"""
        self._pause_event.set()

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
        time.sleep(config.CRAWL_CONFIG["settle_seconds"] * 3)  # 多等，让活动弹窗(更新等)弹出来
        self._dismiss_popups()  # 关启动后的活动弹窗(检测到更新等)

    def _dismiss_popups(self, max_rounds=4):
        """关闭启动后的活动弹窗(检测到更新/优惠券/新人福利等)。

        OCR 找弹窗关闭按钮(以后再说/暂不/知道了/关闭/取消)，点到没有为止。
        ⚠️ "立即使用/立即升级"这类会跳转离开首页，不点。只点明确的"关闭类"按钮。
        弹窗若在中下部(cy>1000)且不挡顶部相机，可忽略。
        ⚠️ 弹窗可能延迟弹出，故前 2 轮没找到也继续(第 2 轮再确认)。
        """
        close_words = ["以后再说", "暂不升级", "暂不", "知道了", "关闭", "取消",
                       "下次再说", "忽略", "稍后"]
        for i in range(max_rounds):
            items = self._shot_ocr()
            btn = next((it for it in items if any(w in it["text"] for w in close_words)), None)
            if btn:
                self.logger.info(f"关闭弹窗：点 '{btn['text']}' @({int(btn['cx'])},{int(btn['cy'])})")
                self.adb.tap(btn["cx"], btn["cy"])
                time.sleep(2)
            else:
                self.logger.info(f"第{i+1}轮未检测到弹窗关闭按钮")
                if i >= 1:
                    return  # 连续 2 轮没弹窗，认为没弹窗
        self.logger.warning("仍有弹窗，可能需手动关")

    def enter_scan(self):
        """进入拍同款：在 livelite 首页点搜索框右侧的相机图标。

        ScanCommodityActivity 无法从外部 am start（会回桌面），必须走 UI。
        策略：OCR 找顶部「搜索」按钮作锚点 → 相机在其左侧约 30dp(基准 480dpi 下≈90px) → 点击 → 验证出现「相册/识别」。
        （已真机验证：搜索按钮 cx≈1289，相机 ≈ left-90 = 1141，点击后进入拍同款页）
        """
        # 等首页加载 + 找"搜索"按钮：取最右侧的(真正的搜索按钮，排除左侧搜索框占位文字)
        anchor = None
        end = time.time() + 12
        while time.time() < end:
            items = self._shot_ocr()
            candidates = [it for it in items if "搜索" in it["text"] and it["score"] >= 0.5]
            if candidates:
                anchor = max(candidates, key=lambda x: x["cx"])  # 最右 = 搜索按钮
                break
            time.sleep(1.5)
        if not anchor:
            self.logger.warning("未找到顶部'搜索'按钮，无法定位相机入口")
            return False
        # 找到搜索 = 首页加载完，此时活动弹窗(检测到更新等)也弹出来了，关掉再点相机
        self._dismiss_popups()
        # 弹窗关后重新确认搜索位置(避免弹窗遮挡导致坐标偏移)
        items2 = self._shot_ocr()
        cand2 = [it for it in items2 if "搜索" in it["text"] and it["score"] >= 0.5]
        if cand2:
            anchor = max(cand2, key=lambda x: x["cx"])
        cam_x = anchor["left"] - self._px["camera_left"]
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
        """把本地待搜索图片推送到手机相册目录，并确保它在相册最前(最新)。

        相册按图片 mtime 降序排列(最新在最前)。若手机相册里有比 sample.jpg 更新的图，
        sample.jpg 会被挤到后面、选图点到错图。所以推送前更新本地时间戳为当前、
        推送后再 touch 设备文件，保证它 mtime 最新、排第一。
        """
        src = self.search_image_path
        if not os.path.isfile(src):
            raise FileNotFoundError(f"待搜索图片不存在: {src}")
        dev_dir = config.APP_CONFIG["device_image_dir"]
        dev_path = dev_dir + os.path.basename(src)
        os.utime(src, None)  # 本地 mtime 设为当前，push 后设备端继承为最新
        self.logger.info(f"推送图片: {src} -> {dev_path}")
        self.adb.push(src, dev_dir)
        try:
            self.adb.shell(f"touch {dev_path}")  # 兜底：强制设备端 mtime 最新
        except Exception:
            pass
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
                if it["bottom"] <= price_it["top"] + self._px["title_above_dy"] and it["cx"] < price_it["right"] + self._px["title_right_dx"]:
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
                    if abs(it["cy"] - price_it["cy"]) < self._px["shop_cy_tol"]:
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
        """下滑详情页用模板匹配找「参数入口图标」，返回其位置 {cx, cy}。

        参数入口最左边有个图标(表盘样式)，OCR 读不到文字，用 OpenCV matchTemplate
        定位——不依赖"退货包邮券"等文字锚点，适配所有商品布局差异。
        ⚠️ 模板 templates/param_row.png 需与当前设备同分辨率截图裁剪。
        """
        max_scrolls = max_scrolls or config.DETAIL_CONFIG["max_scrolls_find_params"]
        # 所有 param_icon*.png 模板都试(不同商品图标有颜色/细节/缩放变体，单模板匹配度差)
        import glob
        tpls = sorted(glob.glob(os.path.join(self.tmpl.template_dir, "param_icon*.png")))
        w = self.adb.window_size()["w"]
        for i in range(max_scrolls):
            self._check_stop()
            shot = self._shot()
            # 主方案：表盘图标模板匹配
            for tpl in tpls:
                pos = self.tmpl.find(shot, os.path.basename(tpl))
                if pos:
                    self.logger.info(f"第{i+1}屏匹配 {os.path.basename(tpl)} @({int(pos[0])},{int(pos[1])})")
                    return {"cx": pos[0], "cy": pos[1]}
            # 备用方案：没匹配到表盘图标(少数商品无此图标)，OCR 找表面参数行当入口点击
            items = self.ocr.recognize(shot)
            keys = [it for it in items if self._is_param_key(it)]
            if len(keys) >= 2:
                cy = int(sum(k["cy"] for k in keys) / len(keys))
                self.logger.info(f"第{i+1}屏 表盘图标未匹配，改点表面参数行 @(cx{int(w*0.5)},cy{cy})")
                return {"cx": w * 0.5, "cy": cy}
            self.adb.swipe_up()
            time.sleep(config.CRAWL_CONFIG["settle_seconds"])
        self.logger.warning(f"滑动 {max_scrolls} 次未匹配到参数入口图标")
        return None

    def _is_param_key(self, it):
        """单项是否像参数 key(含参数名，排除卖点/操作/标签/容器标题/多参数合并摘要)。"""
        t = it["text"]
        if any(kw in t for kw in locators.PARAM_SELLER_WORDS):
            return False
        if any(kw in t for kw in ["客服", "购物车", "下单", "产品参数", "商品参数",
                                    "查看", "领取", "进入", "关闭", "更多", "详情"]):
            return False
        # 排除"电压·型号·品牌"这类多个参数名用 ·/| 连接的合并摘要(非单一参数 key)
        # 及"充气噪音<40dB"这类带 < > 数值描述的卖点
        if "·" in t or "|" in t or "<" in t or ">" in t:
            return False
        # PARAM_KEY_HINTS 命中即为 key；或用户输入的参数容器关键词命中(用户关键词
        # 本身就是该品类的参数键，在完整参数页里也应识别为 key 参与配对)
        return (any(kw in t for kw in locators.PARAM_KEY_HINTS)
                or any(kw in t for kw in self.params_keywords))

    def collect_params_full(self, items):
        """完整参数页键值对配对：每个 key(含参数名) 找最近 value。

        完整参数页布局：多数参数「key 左 + value 右」同行；前几行网格「value 上 +
        key 下」。对每个 key 优先配同行右侧(cx 更大)的非 key 项，其次配正上方。
        每个 value 只用一次(used 去重)，避免多 key 抢同一 value。
        """
        keys = [it for it in items if self._is_param_key(it)]
        params = {}
        used = set()
        # 配对容差(已按设备 dpi 换算 px，来自 DETAIL_CONFIG 的 full_kv_* 字段，单位原为 dp)
        sr_dy = self._px["full_kv_same_row_dy"]
        sr_dx_lo, sr_dx_hi = self._px["full_kv_same_row_dx"]
        ab_dy_lo, ab_dy_hi = self._px["full_kv_above_dy"]
        ab_dx = self._px["full_kv_above_dx"]
        for k in keys:
            cands = []
            for v in items:
                if v is k or id(v) in used:
                    continue
                if self._is_param_key(v):
                    continue
                dcy = v["cy"] - k["cy"]
                dcx = v["cx"] - k["cx"]
                if abs(dcy) < sr_dy and sr_dx_lo < dcx < sr_dx_hi:   # 同行右侧(key左 value右)
                    cands.append((v, dcx))
                elif ab_dy_lo < dcy < ab_dy_hi and abs(dcx) < ab_dx:  # 正上方(value上 key下)
                    cands.append((v, 100000 + abs(dcx)))
            if cands:
                cands.sort(key=lambda x: x[1])
                params[k["text"].strip()] = cands[0][0]["text"].strip()
                used.add(id(cands[0][0]))
        return params

    def _find_params_entry_by_keywords(self, keywords, items):
        """用用户关键词在详情页 OCR 结果里定位「参数入口卡片」位置 {cx, cy, text}。

        参数入口卡片右侧显示参数摘要(多个参数键用「·」/「|」/空格分隔，如
        "维修方式·上市时间·机身厚度·电池容量…")。用户传入该品类的参数键关键词
        (前端文本框，逗号分隔)，命中即定位到摘要行 → 点击该行进完整参数页。

        打分：优先「含分隔符的摘要行」(典型参数摘要特征)，其次「命中关键词数最多」
        的行——避免误匹配碰巧含关键词的商品标题/卖点。无关键词或未命中返回 None。
        """
        if not keywords:
            return None
        best = None  # (是否含分隔符, 命中关键词数), item
        for it in items:
            t = it.get("text", "")
            if not t:
                continue
            hits = sum(1 for kw in keywords if kw and kw in t)
            if hits == 0:
                continue
            has_sep = ("·" in t) or ("|" in t)
            score = (1 if has_sep else 0, hits)
            if best is None or score > best[0]:
                best = (score, it)
        if best is None:
            return None
        it = best[1]
        self.logger.info(
            f"关键词{keywords} 命中参数摘要「{it['text'][:24]}」 @({int(it['cx'])},{int(it['cy'])})"
        )
        return {"cx": it["cx"], "cy": it["cy"], "text": it["text"]}

    def collect_full_params_page(self):
        """进完整参数页收集全部参数，返回键值对 dict。

        定位「参数入口卡片」优先级：
          1) 用户关键词(前端/CLI 传入)：OCR 找含关键词的参数摘要行 → 点该行进参数页。
             最鲁棒，通吃齿轮/列表/表盘等所有图标样式(靠卡片上的参数文字，图标再变也不怕)。
          2) 图标模板兜底：未提供关键词、或关键词未命中/点击未进时，用 param_icon*.png
             模板匹配找入口图标(param_icon=表盘, param_icon3=齿轮…)。
        进参数页后用"产品参数"标题验证(见 _collect_params_from_full)，再上滑收集。
        """
        import glob
        tpls = sorted(glob.glob(os.path.join(self.tmpl.template_dir, "param_icon*.png")))
        max_scrolls = config.DETAIL_CONFIG["max_scrolls_find_params"]
        for i in range(max_scrolls):
            self._check_stop()
            shot = self._shot()
            # 1) 关键词定位参数入口(优先)：仅在用户提供了关键词时才做 OCR 匹配
            if self.params_keywords:
                items = self.ocr.recognize(shot)
                entry = self._find_params_entry_by_keywords(self.params_keywords, items)
                if entry:
                    cx, cy = entry["cx"], entry["cy"]
                    self.logger.info(f"第{i+1}屏 点关键词摘要行进参数页 @({int(cx)},{int(cy)})")
                    self.adb.tap(cx, cy)
                    time.sleep(config.DETAIL_CONFIG["detail_settle_seconds"])
                    params = self._collect_params_from_full()
                    if params is not None:
                        # 进了完整参数页并收集完，关闭完整页返回
                        self.adb.back()
                        time.sleep(config.CRAWL_CONFIG["settle_seconds"])
                        return params
                    # 点摘要行没进参数页：大概率文字不可点/仍停在详情，不 back(避免多退到相册)
                    self.logger.info("  关键词点击未进参数页，转图标模板兜底")
                    time.sleep(0.5)
            # 2) 图标模板兜底：find_all 取多候选(param_icon*.png 全部模板)，逐个点击验证
            candidates = []
            for tpl in tpls:
                candidates.extend(self.tmpl.find_all(shot, os.path.basename(tpl)))
            if candidates:
                candidates.sort(key=lambda x: -x[2])  # 相似度降序
                self.logger.info(f"第{i+1}屏 图标候选 {len(candidates)} 个，逐个验证")
                for cx, cy, score in candidates:
                    self.logger.info(f"  点击候选 @({int(cx)},{int(cy)}) score={score:.2f}")
                    self.adb.tap(cx, cy)
                    time.sleep(config.DETAIL_CONFIG["detail_settle_seconds"])
                    params = self._collect_params_from_full()
                    if params is not None:
                        self.adb.back()
                        time.sleep(config.CRAWL_CONFIG["settle_seconds"])
                        return params
                    # 没进完整页：不 back！点击没进通常是无响应(还停在详情)，再 back 会把
                    # 详情→列表退掉，叠加 collect_detail 末尾的 back 就多退到相册。直接试下个。
                    time.sleep(0.5)
            self.adb.swipe_up()
            time.sleep(config.CRAWL_CONFIG["settle_seconds"])
        self.logger.warning("未成功进入完整参数页")
        return {}

    def _collect_params_from_full(self):
        """当前屏若在完整参数页(有"产品参数"标题)，配对标题下方参数 + 上滑收集，返回 dict；
        不在完整页(无标题)返回 None，供调用方判断是否换候选重试。"""
        items = self._shot_ocr()
        title = self.ocr.find_text(items, ["产品参数", "商品参数", "规格参数", "配置参数"])
        if not title:
            title = next((it for it in items
                          if "参数" in it["text"] and it["cy"] < self.adb.dp(300)), None)
        if not title:
            return None  # 不在完整参数页
        min_cy = int(title["bottom"])
        params = dict(self.collect_params_full([it for it in items if it["cy"] > min_cy]))
        self.logger.info(f"进入完整参数页 ✓ 第1屏 {len(params)} 项")
        for j in range(5):
            self.adb.swipe_up()
            time.sleep(config.CRAWL_CONFIG["settle_seconds"])
            items2 = self._shot_ocr()
            t2 = (self.ocr.find_text(items2, ["产品参数", "商品参数", "规格参数", "配置参数"])
                  or next((it for it in items2
                           if "参数" in it["text"] and it["cy"] < self.adb.dp(300)), None))
            mc = int(t2["bottom"]) if t2 else min_cy
            batch = self.collect_params_full([it for it in items2 if it["cy"] > mc])
            new = {k: v for k, v in batch.items() if k not in params}
            params.update(batch)
            if not new and j > 0:
                break
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
        dy_min, dy_max = self._px["param_row_dy"]
        # 排除参数容器标签本身(产品参数/商品参数/规格参数等)——它是容器标题不是
        # 参数项，不排除会出现"产品参数→退货包邮卷"这类错配
        items = [it for it in items
                 if not any(kw in it["text"] for kw in locators.DETAIL_PARAMS_REGION)]
        # 1. 按 cy 聚类成行
        rows = []
        for it in sorted(items, key=lambda x: x["cy"]):
            placed = False
            for r in rows:
                if abs(it["cy"] - r["cy"]) < self._px["param_row_cy_tol"]:
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
                best, best_d = None, self._px["param_cx_tol"] + 1
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
        dy_lo, dy_hi = self._px["title_search_dy"]
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
            if it["cy"] - groups[-1][-1]["cy"] < self._px["title_merge_dy"]:
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
        # 原价 = 第一个非"券后/新人价"的价格；券后价 = 第一个含"券后/新人价"
        origin_price = ""
        coupon_price = ""
        for p in prices:
            if any(k in p["text"] for k in ["券后", "卷后", "新人价"]):
                coupon_price = coupon_price or p["text"]
            else:
                origin_price = origin_price or p["text"]
        if not origin_price:
            origin_price = prices[0]["text"] if prices else price_item["text"]
        shop = ""
        shop_noise = ["销量", "好评", "评价", "客服", "购物", "售后", "人付款"]
        # 店铺：按优先级(旗舰 > 官方 > 自营/专营/专卖)找，排除销量/好评等文案
        for kw in ["旗舰", "官方", "自营", "专营", "专卖"]:
            for it in screen:
                if kw in it["text"] and not any(n in it["text"] for n in shop_noise):
                    shop = it["text"]
                    break
            if shop:
                break
        # 第0屏漏店铺时，重新 OCR 一次找(OCR 有随机性，"抖音旗舰/旗舰店"偶尔漏识别)
        if not shop:
            for it in self._shot_ocr():
                if any(kw in it["text"] for kw in ["旗舰", "官方", "自营", "专营", "专卖"]) \
                        and not any(n in it["text"] for n in shop_noise):
                    shop = it["text"]
                    break
        # 进完整参数页(点参数容器)，上滑收集全部参数(键值对)
        params = self.collect_full_params_page()
        self.logger.info(
            f"详情抓取：标题='{title[:30]}' 原价={origin_price} 券后={coupon_price} "
            f"店铺={shop} 参数{len(params)}项"
        )
        # 返回列表页：先验证确实在详情(底部有 客服/购物车/旗舰店 操作栏)。
        # 完整参数页若是全屏页，collect_full_params_page 的 back 可能已退到列表，
        # 这里再 back 会多退成 列表→拍同款→相册，故不在详情就不 back。
        items = self._shot_ocr()
        in_detail = any(
            any(k in it["text"] for k in ["客服", "购物车", "旗舰店", "加入购物"])
            and it["cy"] > self.adb.dp(600) for it in items
        )
        if in_detail:
            self.adb.back()
            time.sleep(config.CRAWL_CONFIG["settle_seconds"])
        else:
            self.logger.info("当前不在详情(完整页back已退到列表)，跳过back避免多退到相册")
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
        try:
            for i in range(config.CRAWL_CONFIG["max_scroll_times"]):
                self._check_stop()
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
        except CrawlerStopped:
            self.logger.info(f"用户终止抓取，保留已抓 {len(all_goods)} 件")
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

    def _reenter_list(self):
        """重新进入拍同款结果列表。

        collect_detail 的 back 链(back 关完整页 + back 返回列表)偶发退过头，落到
        相册/拍同款首页。run_detail 检测到当前屏无价格时调用，重走完整流程回到列表。
        """
        self.logger.info("重进拍同款结果列表...")
        self.start_app()
        if not self.enter_scan():
            return
        self.push_image()
        self.upload_image()
        self._wait_text(["已找到", "相似", "同款", "商品"] + locators.PRICE_SYMBOLS, timeout=20)

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
        seen_prices = set()   # 已抓过的价格(归一化为数字，避免 ¥/￥/券后价 差异导致重复)
        idle_scrolls = 0      # 连续未抓到新商品的下滑次数(防死循环)
        w = self.adb.window_size()["w"]

        def _norm_price(t):
            """归一化价格文本：提取数字，忽略 ¥/￥/券后价/新人价/起 等差异。"""
            m = re.search(r"[\d.]+", t)
            return m.group(0) if m else t.strip()

        try:
            while len(results) < max_goods and idle_scrolls < 4:
                self._check_stop()
                items = self._shot_ocr()
                all_prices = self.ocr.find_prices(items)
                # 当前屏无价格 = 不在结果列表(collect_detail 的 back 链可能退过头到相册/
                # 拍同款首页)，重进拍同款搜索回列表，避免一直卡在相册空转
                if not all_prices:
                    self.logger.warning("当前屏无价格(疑似退到相册/拍同款)，重进结果列表...")
                    self._reenter_list()
                    idle_scrolls = 0
                    continue
                # 只点左半屏商品(避开右下直播入口)，cx>50 排除屏幕左边缘误识别
                prices = [p for p in all_prices if 50 < p["cx"] < w * 0.5]
                p = next((x for x in prices if _norm_price(x["text"]) not in seen_prices), None)
                self.logger.info(
                    f"列表扫描: 识别价格{len(all_prices)}个(左半屏{len(prices)}个) "
                    f"{[x['text'] + '@cx' + str(int(x['cx'])) for x in all_prices]} | "
                    f"已抓{seen_prices} | {'→点击' + p['text'] if p else '→下滑'}"
                )
                if not p:
                    # 当前屏无未抓商品：先重 OCR 一次(PaddleOCR 偶发漏识别价格，
                    # 否则会漏抓当前屏剩余商品就下滑)，确认确实没有才下滑
                    items2 = self._shot_ocr()
                    prices2 = [x for x in self.ocr.find_prices(items2)
                               if 50 < x["cx"] < w * 0.5]
                    p = next((x for x in prices2 if _norm_price(x["text"]) not in seen_prices), None)
                    if p:
                        self.logger.info("重OCR 发现漏识别的新商品价格")
                    if not p:
                        self.adb.swipe_up()
                        time.sleep(config.CRAWL_CONFIG["settle_seconds"])
                        idle_scrolls += 1
                        continue
                seen_prices.add(_norm_price(p["text"]))
                self.logger.info(f"=== 第 {len(results)+1}/{max_goods} 个商品: {p['text']} ===")
                try:
                    results.append(self.collect_detail(p))
                    idle_scrolls = 0  # 抓到新商品，重置空闲计数
                except Exception as e:
                    self.logger.warning(f"商品详情抓取失败: {e}")
                    self.adb.back()  # 兜底返回列表
                    time.sleep(config.CRAWL_CONFIG["settle_seconds"])
        except CrawlerStopped:
            self.logger.info(f"用户终止抓取，保留已抓 {len(results)} 件")
        return results
