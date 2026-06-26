# -*- coding: utf-8 -*-
"""
web/server.py
================================================================================
抖音抓取前端 Web 服务（FastAPI + SSE）。
================================================================================
职责：
  - 提供前端单页（GET /）。
  - 图片上传（POST /api/image）→ 存为待搜索图片。
  - 启动抓取（POST /api/start）→ 后台线程跑 DouyinCrawler，结果存到指定 Excel 目录。
  - 实时事件流（GET /api/events，SSE）→ 推 log / status / shot 给前端浏览器。
  - 结果下载（GET /api/download?file=excel|json|txt）。

设计：
  - 核心爬虫（douyin_crawler.py）逻辑零改动，复用 DouyinCrawler + main.save_results/save_excel。
  - 同步阻塞的 DouyinCrawler 跑在 threading.Thread，不卡住 async server。
  - 一台手机同一时刻只允许一个抓取（CrawlerState 单例）。
  - 日志/截图通过广播队列推给所有 SSE 订阅者。

启动：
  uv run uvicorn web.server:app --port 8010
  或 uv run python -m web.server
"""

import os
import sys
import json
import time
import queue
import logging
import threading
import asyncio
from typing import Optional

# 确保能 import 项目根的 config / douyin_crawler / main
_BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

import config
from douyin_crawler import DouyinCrawler
import main as cli_main  # 复用 save_results / save_excel

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(_UPLOAD_DIR, exist_ok=True)
_ALLOWED_IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


# ==============================================================================
# CrawlerState：全局抓取状态（单例）
# ==============================================================================
class CrawlerState:
    """全局抓取状态 + SSE 订阅者广播。

    一台手机同一时刻只跑一个抓取。state 字段在 GIL 下简单赋值是原子的，
    订阅者列表用 _lock 保护。事件通过无界 queue.Queue 广播（不会因满丢失）。
    """

    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"

    def __init__(self):
        self.state: str = self.IDLE
        self.error: Optional[str] = None
        self.excel_path: Optional[str] = None
        self.json_path: Optional[str] = None
        self.txt_path: Optional[str] = None
        self.goods_count: int = 0
        self._subscribers: list = []
        self._lock = threading.Lock()
        self._last_shot_mtime: float = 0.0

    def subscribe(self) -> queue.Queue:
        """新建一个 SSE 订阅队列并注册。"""
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        """SSE 断开时注销队列。"""
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def broadcast(self, event: dict):
        """向所有订阅者广播一个事件（非阻塞，无界队列不会 Full）。"""
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass  # 无界队列理论上不会触发


STATE = CrawlerState()


# ==============================================================================
# SseLogHandler：把 DouyinCrawler 日志推给 SSE
# ==============================================================================
class SseLogHandler(logging.Handler):
    """自定义 logging handler：把 LogRecord 转成 log 事件广播给 SSE 订阅者。"""

    def emit(self, record: logging.LogRecord):
        try:
            STATE.broadcast({
                "type": "log",
                "ts": record.created,
                "level": record.levelname,
                "msg": record.getMessage(),
            })
        except Exception:
            # logging handler 的 emit 绝不能抛异常（否则干扰主流程）
            pass


# ==============================================================================
# FastAPI app
# ==============================================================================
app = FastAPI(title="抖音抓取控制台")
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    """返回前端单页。"""
    with open(os.path.join(_STATIC_DIR, "index.html"), "r", encoding="utf-8") as f:
        return f.read()


@app.post("/api/image")
async def upload_image(file: UploadFile = File(...)):
    """上传待搜索图片，存到 web/uploads/current.<ext>，返回本地路径 + 预览 URL。"""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in _ALLOWED_IMG_EXT:
        raise HTTPException(400, f"仅支持图片格式: {sorted(_ALLOWED_IMG_EXT)}")
    data = await file.read()
    if not data:
        raise HTTPException(400, "上传内容为空")
    dest = os.path.join(_UPLOAD_DIR, "current" + ext)
    with open(dest, "wb") as f:
        f.write(data)
    return {
        "path": dest,
        "preview_url": f"/api/uploads/current{ext}?t={int(time.time())}",
    }


@app.get("/api/uploads/{name}")
async def get_upload(name: str):
    """返回上传的图片（前端预览用）。"""
    if not _is_safe_name(name):
        raise HTTPException(400, "非法文件名")
    path = os.path.join(_UPLOAD_DIR, name)
    if not os.path.isfile(path):
        raise HTTPException(404, "文件不存在")
    return FileResponse(path)


@app.post("/api/start")
async def start_crawl(request: Request):
    """启动抓取（后台线程）。body: {mode, max_goods, excel_dir, image_path}。"""
    if STATE.state == CrawlerState.RUNNING:
        raise HTTPException(409, "已有抓取在运行，请等待完成")
    body = await request.json()
    mode = body.get("mode", "list")
    if mode not in ("list", "detail"):
        raise HTTPException(400, "mode 必须是 list 或 detail")
    max_goods = int(body.get("max_goods", 5))
    excel_dir = body.get("excel_dir") or config.OUTPUT_CONFIG["output_dir"]
    image_path = body.get("image_path")

    if not image_path or not os.path.isfile(image_path):
        raise HTTPException(400, "请先上传待搜索图片")
    if not _dir_writable(excel_dir):
        raise HTTPException(400, f"Excel 目录不可写: {excel_dir}")

    threading.Thread(
        target=_run_crawl,
        args=(mode, max_goods, excel_dir, image_path),
        daemon=True,
    ).start()
    return {"status": "started", "state": CrawlerState.RUNNING}


def _run_crawl(mode: str, max_goods: int, excel_dir: str, image_path: str):
    """后台线程：构造 crawler、挂 SSE 日志、跑抓取、保存结果、更新状态。

    任何异常都捕获并广播 error 状态，绝不让线程静默崩。SSE 日志 handler 仅在此
    抓取期间挂载，结束后摘除。
    """
    STATE.state = CrawlerState.RUNNING
    STATE.error = None
    STATE.goods_count = 0
    STATE._last_shot_mtime = 0.0  # 重置截图基线，让本次运行的截图重新推送
    STATE.broadcast({"type": "status", "state": CrawlerState.RUNNING})

    crawler = None
    handlers = []
    try:
        crawler = DouyinCrawler(search_image_path=image_path)
        # 挂 SSE 日志 handler 到 crawler 及其 adb logger（覆盖流程 + 设备日志）
        h_crawler = _attach_sse_handler(crawler.logger)
        h_adb = _attach_sse_handler(crawler.adb.logger)
        handlers = [h_crawler, h_adb]

        goods = crawler.run_detail(max_goods) if mode == "detail" else crawler.run()

        STATE.goods_count = len(goods)
        if goods:
            cli_main.save_results(goods, output_dir=excel_dir)
            cli_main.save_excel(goods, output_dir=excel_dir)
            STATE.json_path = os.path.join(excel_dir, config.OUTPUT_CONFIG["output_file"])
            STATE.txt_path = os.path.join(excel_dir, config.OUTPUT_CONFIG["txt_file"])
            STATE.excel_path = os.path.join(excel_dir, config.OUTPUT_CONFIG["excel_file"])
        STATE.state = CrawlerState.DONE
        STATE.broadcast({
            "type": "status", "state": CrawlerState.DONE,
            "goods_count": STATE.goods_count,
            "excel": STATE.excel_path, "json": STATE.json_path, "txt": STATE.txt_path,
        })
    except Exception as e:
        logging.getLogger("web.server").exception("抓取线程异常")
        STATE.state = CrawlerState.ERROR
        STATE.error = f"{type(e).__name__}: {e}"
        STATE.broadcast({"type": "status", "state": CrawlerState.ERROR, "error": STATE.error})
    finally:
        for h in handlers:
            if h and crawler:
                crawler.logger.removeHandler(h)
                crawler.adb.logger.removeHandler(h)


def _attach_sse_handler(logger: logging.Logger) -> SseLogHandler:
    """给指定 logger 挂一个 SseLogHandler，返回它便于事后摘除。"""
    h = SseLogHandler()
    logger.addHandler(h)
    return h


@app.get("/api/status")
async def get_status():
    """返回当前状态（轮询备用，主要靠 SSE）。"""
    return {
        "state": STATE.state,
        "goods_count": STATE.goods_count,
        "excel": STATE.excel_path,
        "json": STATE.json_path,
        "txt": STATE.txt_path,
        "error": STATE.error,
    }


@app.get("/api/fs/list")
async def fs_list(dir: str = ""):
    """列出本地目录的子目录（前端目录选择器用）。只返回目录名，不返回文件。

    本地工具（服务跑在本机），故可访问磁盘。dir 为空返回盘符列表(Windows)；
    传入路径返回其子目录 + 父目录。隐藏点开头的目录。
    """
    import string
    if not dir:
        drives = [f"{L}:\\" for L in string.ascii_uppercase if os.path.isdir(f"{L}:\\")]
        return {"current": "", "parent": None, "dirs": drives, "is_root": True}
    target = os.path.abspath(dir)
    if not os.path.isdir(target):
        raise HTTPException(400, f"不是有效目录: {target}")
    try:
        names = sorted(os.listdir(target))
    except PermissionError:
        raise HTTPException(403, f"无权限访问: {target}")
    subdirs = [n for n in names
               if not n.startswith(".") and os.path.isdir(os.path.join(target, n))]
    # 盘符根目录的父级回到盘符列表
    _drive, tail = os.path.splitdrive(target)
    is_root = (tail in ("\\", "/", ""))
    parent = None if is_root else os.path.dirname(target)
    return {"current": target, "parent": parent, "dirs": subdirs, "is_root": is_root}


@app.get("/api/events")
async def events(request: Request):
    """SSE 事件流：推 log / status / shot。心跳维持连接。"""
    q = STATE.subscribe()

    async def stream():
        try:
            # 连上先推一次当前状态
            yield _sse({"type": "status", "state": STATE.state,
                        "goods_count": STATE.goods_count, "error": STATE.error})
            loop = asyncio.get_running_loop()
            while True:
                if await request.is_disconnected():
                    break
                try:
                    # 阻塞读队列放到线程池，超时则发心跳（默认 15s）
                    event = await loop.run_in_executor(None, lambda: q.get(timeout=15))
                    yield _sse(event)
                except queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            STATE.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/shot/{name}")
async def get_shot(name: str):
    """返回某张调试截图（前端预览最新截图用）。"""
    if not _is_safe_name(name):
        raise HTTPException(400, "非法文件名")
    path = os.path.join(config.OUTPUT_CONFIG["screenshot_dir"], name)
    if not os.path.isfile(path):
        raise HTTPException(404, "截图不存在")
    return FileResponse(path)


@app.get("/api/download")
async def download(file: str):
    """下载结果文件：file=excel|json|txt。"""
    path = {"excel": STATE.excel_path, "json": STATE.json_path, "txt": STATE.txt_path}.get(file)
    if not path or not os.path.isfile(path):
        raise HTTPException(404, "文件不存在（可能尚未抓取完成）")
    return FileResponse(path, filename=os.path.basename(path))


# ==============================================================================
# 辅助函数
# ==============================================================================
def _sse(event: dict) -> str:
    """把事件 dict 编码成一条 SSE data 帧。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _is_safe_name(name: str) -> bool:
    """防路径穿越：只允许纯文件名（无目录分隔/..）。"""
    return bool(name) and "/" not in name and "\\" not in name and ".." not in name


def _dir_writable(path: str) -> bool:
    """检查目录可写（不存在则尝试创建）。"""
    try:
        os.makedirs(path, exist_ok=True)
        testf = os.path.join(path, ".wtest")
        with open(testf, "w") as f:
            f.write("x")
        os.remove(testf)
        return True
    except OSError:
        return False


def _shot_monitor():
    """后台线程：每秒检查 output/screenshots 最新截图，有更新则广播 shot 事件。

    仅在抓取运行期间推送，避免空闲时持续磁盘 IO。
    """
    while True:
        time.sleep(1.0)
        if STATE.state != CrawlerState.RUNNING:
            continue
        shot_dir = config.OUTPUT_CONFIG["screenshot_dir"]
        if not os.path.isdir(shot_dir):
            continue
        try:
            shots = [os.path.join(shot_dir, f) for f in os.listdir(shot_dir)
                     if f.startswith("shot_") and f.endswith(".png")]
        except OSError:
            continue
        if not shots:
            continue
        latest = max(shots, key=os.path.getmtime)
        mtime = os.path.getmtime(latest)
        if mtime > STATE._last_shot_mtime:
            STATE._last_shot_mtime = mtime
            STATE.broadcast({
                "type": "shot",
                "url": f"/api/shot/{os.path.basename(latest)}?t={int(mtime)}",
            })


# 启动截图监控后台线程（daemon，随进程退出）
threading.Thread(target=_shot_monitor, daemon=True).start()


if __name__ == "__main__":
    # 直接 python -m web.server 时运行（生产用 uvicorn web.server:app --port N）
    # 默认 8010：本机 8000 被 WSL(wslhost) 占用；可用环境变量 DOUYIN_WEB_PORT 覆盖
    port = int(os.environ.get("DOUYIN_WEB_PORT", "8010"))
    uvicorn.run("web.server:app", host="127.0.0.1", port=port, reload=False)
