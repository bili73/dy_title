# -*- coding: utf-8 -*-
"""
launcher.py
================================================================================
抖音拍同款抓取 - 启动器(打包成 exe 的入口)
================================================================================
双击 exe 流程:
  1. 检测 Docker 是否运行(没运行提示先启动 Docker Desktop)
  2. 检测 OCR 容器(dev-paddleocr): 在跑->跳过; 已建->start; 没有->docker load 同目录
     paddleocr.tar + docker run 创建
  3. 等 OCR 服务就绪(健康检查 / running，加载模型约 10-30 秒)
  4. 启动 Web 服务(8010) + 自动打开浏览器

文件夹结构(exe 同目录):
  抖音抓取.exe / paddleocr.tar / adb.exe(+AdbWinApi.dll 等) / images/ / README.txt
"""
import os
import sys
import time
import subprocess
import threading
import webbrowser

import requests

# Windows console 默认 GBK，✓✗→ 等 Unicode 符号 + 中文混合会 UnicodeEncodeError 让 exe 崩。
# 切 console codepage 到 utf-8 + reconfigure stdout/stderr utf-8，中文/符号都正常显示。
try:
    subprocess.run(["cmd", "/c", "chcp", "65001"], capture_output=True, check=False)
except Exception:
    pass
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

OCR_CONTAINER = "dev-paddleocr"   # OCR 容器名(与抓取端 OCR_CONFIG 对应)
OCR_IMAGE = "dev-paddleocr"       # docker load 后的镜像名
OCR_PORT = 9300
WEB_PORT = 8010


def app_dir():
    """exe/脚本所在目录(找同目录 paddleocr.tar / adb.exe)。
    PyInstaller 打包后用 sys.executable，开发时用 __file__。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _run(cmd):
    """跑命令，返回 CompletedProcess(capture stdout/stderr, text)。"""
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def docker_ok():
    """Docker 守护进程是否在跑。"""
    return _run(["docker", "info"]).returncode == 0


def container_state(name):
    """容器状态: 'running' / 'exists'(已建未跑) / 'none'(没有)。"""
    r = _run(["docker", "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"])
    if name in (r.stdout or "").split():
        return "running"
    r = _run(["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"])
    if name in (r.stdout or "").split():
        return "exists"
    return "none"


def image_exists(image):
    """镜像是否已加载。"""
    r = _run(["docker", "images", "--format", "{{.Repository}}", image])
    return image in (r.stdout or "").split()


def _pause_exit():
    """报错后暂停等回车(避免 exe 闪退看不到错误)。"""
    try:
        input("\n按回车退出...")
    except EOFError:
        pass


def ensure_ocr(d):
    """确保 OCR 容器运行。返回 True/False。需 docker + 镜像(或同目录 tar)。"""
    if not docker_ok():
        print("[ERR] Docker 未运行！请先启动 Docker Desktop，等它完全起来后再双击 exe。")
        _pause_exit()
        return False
    state = container_state(OCR_CONTAINER)
    if state == "running":
        print("[OK] OCR 容器已在运行")
    elif state == "exists":
        print("-> 启动已有 OCR 容器...")
        if _run(["docker", "start", OCR_CONTAINER]).returncode != 0:
            print("[ERR] docker start 失败")
            return False
    else:
        # 没容器 -> 需镜像(没镜像则从同目录 tar 加载)
        if not image_exists(OCR_IMAGE):
            tar = os.path.join(d, "paddleocr.tar")
            if not os.path.isfile(tar):
                print(f"[ERR] 未找到镜像文件: {tar}")
                print("   请把 paddleocr.tar 放到 exe 同目录后重试。")
                _pause_exit()
                return False
            print("-> 加载镜像 paddleocr.tar（首次较慢，请等待）...")
            if _run(["docker", "load", "-i", tar]).returncode != 0:
                print("[ERR] docker load 失败")
                return False
            print("[OK] 镜像加载完成")
        print("-> 创建并启动 OCR 容器...")
        r = _run(["docker", "run", "-d", "--name", OCR_CONTAINER,
                  "-p", f"{OCR_PORT}:9300", OCR_IMAGE])
        if r.returncode != 0:
            print(f"[ERR] docker run 失败: {r.stderr}")
            print("   (若提示端口占用，可能别的程序用了 9300)")
            return False
    # 等 OCR 就绪
    print("-> 等 OCR 服务就绪(加载模型约 10-30 秒)...")
    for _ in range(40):
        try:
            if "running" in requests.get(f"http://localhost:{OCR_PORT}/", timeout=2).text:
                print("[OK] OCR 服务就绪")
                return True
        except Exception:
            pass
        time.sleep(2)
    print("[ERR] OCR 就绪超时(80秒)。查日志: docker logs dev-paddleocr")
    return False


def main():
    """启动器主流程：部署 OCR -> 启动 Web -> 开浏览器。"""
    d = app_dir()
    if d not in sys.path:
        sys.path.insert(0, d)  # 让 import web.server/config/douyin_crawler 找到项目根

    print("=" * 56)
    print("  抖音拍同款抓取 - 启动器")
    print("=" * 56)
    if not ensure_ocr(d):
        _pause_exit()
        return

    print(f"\n-> 启动 Web 服务(端口 {WEB_PORT})，浏览器即将自动打开...\n")
    # 延迟 2 秒开浏览器(等 web server 起来)
    threading.Thread(
        target=lambda: (time.sleep(2.0), webbrowser.open(f"http://localhost:{WEB_PORT}")),
        daemon=True,
    ).start()
    # 启动 web server(阻塞)。直接 import app 让 PyInstaller 能收集 web.server 依赖
    import uvicorn
    from web.server import app
    uvicorn.run(app, host="127.0.0.1", port=WEB_PORT, reload=False)


if __name__ == "__main__":
    main()
