# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir）。

Windows 产物：dist/EtfBoard/EtfBoard.exe
macOS 产物：  dist/缅A决策看板.app
  - 自带主项目代码（build/core/）与运行依赖，用户机器不需要装 Python；
  - 数据落在用户目录（LOCALAPPDATA / Application Support），安装目录只读。

构建：  python scripts/prepare_core.py
        python -m PyInstaller packaging/etf_board.spec --noconfirm
mac 上产物要签名/公证：见 README「macOS 打包与签名」。
"""

import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent          # noqa: F821 — SPECPATH 由 PyInstaller 注入
CORE = ROOT / "build" / "core"

if not (CORE / "app.py").is_file():
    raise SystemExit("缺少 build/core：先跑 python scripts/prepare_core.py")

datas = [(str(CORE / "web" / "dist"), "web/dist")]

# 关键：把主项目入口当 hiddenimport 交给分析器，才能把它和它 import 的一切
# （fastapi 各中间件、routers、services）都收进 PYZ。壳自己不 import 主项目。
hiddenimports = [
    "app",
    # uvicorn 用字符串导入选实现，PyInstaller 静态分析看不到，必须显式声明
    "uvicorn.logging",
    "uvicorn.loops.auto", "uvicorn.loops.asyncio", "uvicorn.loops.uvloop",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto", "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on", "uvicorn.lifespan.off",
]

# 窗口内核 / 托盘后端按平台选：写全会把另一平台的二进制塞进包，还可能导入失败
if sys.platform == "win32":
    hiddenimports += [
        "webview.platforms.edgechromium", "webview.platforms.winforms",
        "pystray._win32",
    ]
elif sys.platform == "darwin":
    hiddenimports += ["webview.platforms.cocoa"]      # mac 用菜单栏，不走 pystray

a = Analysis(
    # 第一个是真正的入口；_warm.py 只用于兜底收集三方依赖
    [str(ROOT / "launcher" / "main.py"), str(ROOT / "packaging" / "_warm.py")],
    pathex=[str(ROOT), str(CORE)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "setuptools", "pip", "matplotlib", "numpy", "pandas"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="EtfBoard",
    debug=False,
    strip=False,
    upx=False,
    console=False,          # 窗口模式：没有黑框；日志写 %LOCALAPPDATA%\EtfBoard\launcher.log
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="EtfBoard",
)

# macOS：PyInstaller 的 onedir 在 mac 上要再包一层 .app，否则没有 Dock 图标、
# 也没法被 Gatekeeper 认。Windows 上跳过这段。
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        # 文件名用 ASCII：中文路径在 CI/签名/命令行里到处是坑，
        # Dock 上显示的名字由 CFBundleDisplayName 决定
        name="EtfBoard.app",
        icon=None,
        bundle_identifier="com.niosai.etfboard",
        info_plist={
            "CFBundleName": "缅A决策看板",
            "CFBundleDisplayName": "缅A决策看板",
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1.0.0",
            "NSHighResolutionCapable": True,
            # 只连本机 127.0.0.1，不触发「允许传入连接」弹窗
            "LSApplicationCategoryType": "public.app-category.finance",
            "LSMinimumSystemVersion": "11.0",
        },
    )
