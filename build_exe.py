# -*- coding: utf-8 -*-
"""
build_exe.py
================================================================================
PyInstaller 打包脚本：把 launcher + crawler + web 打包成 exe(文件夹模式)。
================================================================================
用法:
  uv run pip install pyinstaller      # 先装 PyInstaller(一次性)
  uv run python build_exe.py          # 打包

输出: dist/抖音抓取/抖音抓取.exe + _internal/(依赖)
打包后手动放进 dist/抖音抓取/ 同目录:
  - paddleocr.tar   (docker save -o paddleocr.tar dev-paddleocr 导出)
  - adb.exe + AdbWinApi.dll + AdbWinUsbApi.dll  (从 platform-tools 复制)
  - images/sample.jpg  (默认搜索图，或运营前端上传)
然后把整个 dist/抖音抓取/ 文件夹压缩发给同事。
"""
import PyInstaller.__main__

# uvicorn 子模块是运行时动态 import，PyInstaller 静态分析扫不到，需显式 hidden-import
HIDDEN = [
    "web.server",
    "uvicorn.logging",
    "uvicorn.protocols",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
]

if __name__ == "__main__":
    args = [
        "launcher.py",
        "--name=抖音抓取",
        "--onedir",                 # 文件夹模式(exe + _internal/)，启动快；exe 同目录放 tar/adb
        "--add-data=web;web",       # web 包(server.py + static/ 前端)
        "--add-data=locators.py;.",
        "--noconfirm",
        "--clean",
    ] + [f"--hidden-import={m}" for m in HIDDEN]
    print("打包参数:", args)
    PyInstaller.__main__.run(args)
    print("\n✓ 打包完成: dist/抖音抓取/")
    print("  记得把 paddleocr.tar + adb.exe(+dll) 放进 dist/抖音抓取/ 同目录再压缩发同事。")
