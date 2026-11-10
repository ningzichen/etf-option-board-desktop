"""桌面壳启动器（P2）。

职责边界（严格）：
  只做六件事 —— 单实例、找端口、起后端子进程、开窗口、托盘常驻、退出时收尾。
  不 import 主项目任何模块，只通过 HTTP 与 /api/health 打交道。
  主项目内部怎么重构，这里都不需要跟着改。

关窗行为：**关窗 = 收到托盘**，后端继续跑，告警推送才收得到；
只有托盘菜单里的「退出」才真正结束进程（并停掉后端子进程）。

平台差异（重要）：
  Windows —— pystray 托盘图标 + edgechromium 内核（系统自带 WebView2）。
  macOS   —— 用菜单栏常驻代替托盘。pystray 的 darwin 后端和 pywebview 都要求
             「主线程跑事件循环」，两者放一起会抢主线程，所以 mac 上不用 pystray：
             关窗 = 隐藏窗口（进程不死，推送不断），菜单栏「显示看板」找回窗口。

运行：  .venv\\Scripts\\python launcher\\main.py
打包：  见 packaging/etf_board.spec（入口也是这个文件，产物名 EtfBoard.exe）
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent      # launcher/
ROOT = HERE.parent                          # 桌面壳仓库根
CONF_PATH = ROOT / "desktop.json"
FROZEN = bool(getattr(sys, "frozen", False))    # PyInstaller 打包后为 True

APP_ID = "EtfOptionBoardDesktop"
DEFAULT_CONF = {
    "core_dir": "D:\\GitHub\\etf-option-board",
    "core_python": "D:\\GitHub\\etf-option-board\\.venv\\Scripts\\python.exe",
    "port": 6799,
    "title": "缅A决策看板",
    "width": 1500,
    "height": 940,
    "minimize_to_tray": True,      # 关窗收到托盘（关掉则关窗即退出）
}

READY_TIMEOUT = 60.0      # 后端冷启动上限（首次要建 SQLite / 拉一次行情）
HEALTH_TICK = 0.3

# 进程级状态：单实例句柄要一直挂着，一旦被 GC 锁就没了
_STATE: dict = {"lock": None, "quitting": False, "tray": None, "hinted": False}


# ---------------------------------------------------------------- 配置

def load_conf() -> dict:
    """读 desktop.json；缺字段用默认值补。纯本地文件，绝不写回。"""
    conf = dict(DEFAULT_CONF)
    if CONF_PATH.is_file():
        try:
            conf.update(json.loads(CONF_PATH.read_text(encoding="utf-8")))
        except Exception as exc:      # 配置坏了就退回默认，不让壳起不来
            print(f"[warn] desktop.json 解析失败，使用默认配置：{exc}")
    return conf


def app_data_dir() -> Path:
    """打包版的运行数据目录：Windows 用 LOCALAPPDATA，macOS 用 Application Support。

    主项目装进 exe 后所在目录只读（Program Files / 签名过的 .app），
    所以数据必须外置——靠主项目的 ETF_DATA_DIR 支持（PR #17）。
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "EtfBoard" / "data"


def gui_backend() -> str | None:
    """窗口内核。写死 edgechromium 在 mac 上会直接起不来，必须按平台选。"""
    if sys.platform == "win32":
        return "edgechromium"       # Win10+ 自带 WebView2，不用用户装
    if sys.platform == "darwin":
        return "cocoa"              # 系统 WKWebView
    return None                     # Linux：交给 pywebview 自己挑 gtk/qt


# ---------------------------------------------------------------- 单实例

def acquire_single_instance() -> bool:
    """抢单实例锁。已有实例在跑返回 False。

    Windows 用命名互斥体（Local\\ 作用域 = 当前登录会话），macOS/Linux 用文件锁。
    两者都由内核在进程退出时自动释放，所以崩溃也不会留下死锁。
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        handle = kernel32.CreateMutexW(None, False, f"Local\\{APP_ID}")
        already = ctypes.get_last_error() == 183        # ERROR_ALREADY_EXISTS
        if not handle:                                   # 创建失败不阻塞启动，只记日志
            print(f"[warn] 单实例互斥体创建失败（err={ctypes.get_last_error()}），继续启动")
            return True
        if already:
            kernel32.CloseHandle(handle)
            return False
        _STATE["lock"] = handle
        return True

    import fcntl
    import tempfile

    lock_file = Path(tempfile.gettempdir()) / f"{APP_ID}.lock"
    handle = lock_file.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _STATE["lock"] = handle
    return True


def notify_already_running() -> None:
    """第二个实例：弹个框告诉用户去哪找，然后自己退出。"""
    msg = "看板已经在运行了：窗口关掉后会缩到右下角托盘，双击托盘图标可以重新打开。"
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, msg, DEFAULT_CONF["title"], 0x40)
    else:
        print(f"[info] {msg}")


# ---------------------------------------------------------------- 端口 / 后端

def health_ok(port: int, timeout: float = 1.5) -> bool:
    """端口上是否已经跑着本项目的后端（靠 /api/health 探针）。"""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=timeout
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return bool(data.get("ok"))
    except (urllib.error.URLError, OSError, ValueError):
        return False


def port_free(port: int) -> bool:
    """能否 bind（不占用，只探测）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_port(preferred: int) -> tuple[int, bool]:
    """挑端口。返回 (端口, 是否复用已运行的实例)。

    优先级：首选端口已是我们自己的服务 → 直接复用，不起第二个；
            首选端口空闲 → 用它；
            被别的程序占了 → 往后找 20 个。
    """
    if health_ok(preferred):
        return preferred, True
    if port_free(preferred):
        return preferred, False
    for port in range(preferred + 1, preferred + 21):
        if port_free(port):
            print(f"[info] 端口 {preferred} 被占用，改用 {port}")
            return port, False
    raise SystemExit(f"端口 {preferred}~{preferred + 20} 全被占用，无法启动")


def core_dir_of(conf: dict) -> Path:
    """主项目目录：打包态就是解包目录（代码在 PYZ 里、前端在 web/dist），
    开发态用 desktop.json 指向的外部仓库。"""
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", "."))
    core = Path(conf["core_dir"])
    if not (core / "app.py").is_file():
        raise SystemExit(f"找不到主项目：{core}")
    return core


def backend_env(core: Path) -> dict:
    """子进程环境：打包态把数据目录指到用户目录（安装目录只读、且签名不能改）。"""
    env = os.environ.copy()
    if FROZEN:
        data = app_data_dir()
        data.mkdir(parents=True, exist_ok=True)
        env["ETF_DATA_DIR"] = str(data)      # 主项目 paths.py 认这个变量（PR #17）
        env.setdefault("PYTHONUTF8", "1")    # Windows 控制台默认 GBK，中文日志会炸
    env["PYTHONPATH"] = str(core) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def start_backend(conf: dict, port: int) -> subprocess.Popen:
    """起后端进程。开发态用外部 Python，打包态让 exe 自己以 --serve 再起一份。"""
    core = core_dir_of(conf)
    if FROZEN:
        cmd = [sys.executable, "--serve", "--port", str(port)]
    else:
        python = Path(conf["core_python"])
        if not python.is_file():
            raise SystemExit(f"找不到 Python 解释器：{python}")
        cmd = [
            str(python), "-m", "uvicorn", "app:app",
            "--host", "127.0.0.1",     # 只绑本机：同网段别人打不开，也不会弹防火墙
            "--port", str(port),
        ]
    print(f"[info] 启动后端：{' '.join(cmd)}")
    kwargs = {"cwd": str(core), "env": backend_env(core)}
    if FROZEN:                      # 无控制台：后端的输出也写进同一个日志文件
        log = open_log()
        kwargs.update(stdout=log, stderr=log)
    return subprocess.Popen(cmd, **kwargs)


def log_path() -> Path:
    return app_data_dir().parent / "launcher.log"


def open_log():
    """打包版没有控制台：日志落到 %LOCALAPPDATA%\\EtfBoard\\launcher.log。"""
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("a", encoding="utf-8", buffering=1)


def redirect_logs() -> None:
    """窗口模式下 sys.stdout 是 None，把 print 重定向到日志文件，方便排查。"""
    if not FROZEN:
        return
    try:
        handle = open_log()
        sys.stdout = handle
        sys.stderr = handle
    except Exception:
        pass


def serve(port: int) -> None:
    """打包版的「后端进程」模式：由壳以子进程方式启动同一个 exe。

    打包态直接 import 主项目的 app 对象（代码在 PYZ 里，源码不落盘）；
    开发态仍按模块串加载，和手动起 uvicorn 完全一致。
    """
    conf = load_conf()
    import uvicorn

    if FROZEN:
        import app as core_app            # noqa: PLC0415 — 打包态从 PYZ 加载
        target = core_app.app
    else:
        sys.path.insert(0, str(core_dir_of(conf)))
        target = "app:app"

    uvicorn.run(target, host="127.0.0.1", port=port, log_level="info")


def wait_ready(port: int, proc: subprocess.Popen | None) -> None:
    """等 /api/health 通过；子进程中途死掉就立刻报错，不干等。"""
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise SystemExit(f"后端启动失败（退出码 {proc.returncode}），看上面的日志")
        if health_ok(port):
            return
        time.sleep(HEALTH_TICK)
    raise SystemExit(f"后端 {READY_TIMEOUT:.0f}s 内未就绪，端口 {port}")


def stop_backend(proc: subprocess.Popen | None) -> None:
    """先礼后兵：terminate → 等 5s → kill。复用已有实例时 proc 为 None，不动它。"""
    if proc is None or proc.poll() is not None:
        return
    print("[info] 关闭后端…")
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


# ---------------------------------------------------------------- 托盘

def tray_image():
    """托盘图标：深色圆角底 + 一红一绿两根 K 线（A 股口径，红涨绿跌）。"""
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((2, 2, size - 3, size - 3), radius=14, fill=(11, 15, 20, 255))
    up, down = (226, 75, 74, 255), (29, 158, 117, 255)
    draw.line((21, 15, 21, 22), fill=up, width=3)
    draw.rectangle((16, 22, 26, 44), fill=up)
    draw.line((21, 44, 21, 51), fill=up, width=3)
    draw.line((43, 21, 43, 28), fill=down, width=3)
    draw.rectangle((38, 28, 48, 50), fill=down)
    draw.line((43, 50, 43, 57), fill=down, width=3)
    return img


class Tray:
    """系统托盘：显示窗口 / 打开浏览器 / 退出。不碰后端逻辑。"""

    def __init__(self, window, url: str, title: str, on_quit):
        import pystray

        self.window = window
        self.url = url
        self.on_quit = on_quit
        self._icon = pystray.Icon(
            APP_ID, tray_image(), title,
            menu=pystray.Menu(
                pystray.MenuItem("打开看板", self._show, default=True),
                pystray.MenuItem("在浏览器中打开", self._open_browser),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", self._quit),
            ),
        )

    def start(self) -> None:
        threading.Thread(target=self._icon.run, daemon=True).start()

    def _show(self, *_args) -> None:
        try:
            self.window.show()
            restore = getattr(self.window, "restore", None)   # 最小化过就先还原
            if callable(restore):
                restore()
        except Exception as exc:                              # 窗口已销毁等边界
            print(f"[warn] 显示窗口失败：{exc}")

    def _open_browser(self, *_args) -> None:
        import webbrowser

        webbrowser.open(self.url)

    def _quit(self, *_args) -> None:
        _STATE["quitting"] = True
        try:
            self._icon.stop()
        finally:
            self.on_quit()

    def hide_to_tray(self) -> None:
        """关窗时的动作：藏窗口 + 首次给个气泡提示，后端不动。"""
        try:
            self.window.hide()
        except Exception as exc:
            print(f"[warn] 隐藏窗口失败：{exc}")
        if not _STATE["hinted"]:
            _STATE["hinted"] = True
            try:
                self._icon.notify("看板已缩到托盘，行情采集与告警推送继续运行。", self._icon.title)
            except Exception:                                  # 通知不是关键路径
                pass

    def stop(self) -> None:
        try:
            self._icon.stop()
        except Exception:
            pass


# ---------------------------------------------------------------- macOS 菜单栏

def mac_menu(window, url: str) -> list:
    """macOS 的常驻入口：菜单栏「看板」菜单，替代 Windows 的托盘图标。

    关窗后窗口只是隐藏，进程和后端都还在（告警推送不断），点菜单能找回窗口。
    """
    from webview import menu

    def show() -> None:
        try:
            window.show()
            restore = getattr(window, "restore", None)
            if callable(restore):
                restore()
        except Exception as exc:
            print(f"[warn] 显示窗口失败：{exc}")

    def open_browser() -> None:
        import webbrowser

        webbrowser.open(url)

    def quit_app() -> None:
        _STATE["quitting"] = True
        window.destroy()            # 触发 closing → finally 里收尾后端

    return [
        menu.Menu("看板", [
            menu.MenuAction("显示看板", show),
            menu.MenuAction("在浏览器中打开", open_browser),
            menu.MenuSeparator(),
            menu.MenuAction("退出", quit_app),
        ])
    ]


# ---------------------------------------------------------------- 主流程

def main() -> None:
    redirect_logs()               # 打包版无控制台，先接上日志文件
    argv = sys.argv[1:]
    if "--serve" in argv:          # 打包版的后端子进程模式，先于单实例判断
        idx = argv.index("--port") if "--port" in argv else -1
        serve(int(argv[idx + 1]) if idx >= 0 else int(DEFAULT_CONF["port"]))
        return

    if not acquire_single_instance():
        notify_already_running()
        return

    conf = load_conf()
    port, reused = pick_port(int(conf["port"]))
    url = f"http://127.0.0.1:{port}/"
    proc = None
    tray = None

    try:
        if reused:
            print(f"[info] 端口 {port} 已有实例在跑，直接复用：{url}")
        else:
            proc = start_backend(conf, port)
            wait_ready(port, proc)
            print(f"[info] 后端就绪：{url}")

        import webview  # 延迟导入：只探测端口时不依赖 GUI 库

        window = webview.create_window(
            conf["title"], url,
            width=int(conf["width"]), height=int(conf["height"]),
            min_size=(1100, 720),
            background_color="#0b0f14",
        )
        gui = gui_backend()
        keep_running = bool(conf.get("minimize_to_tray", True))

        if keep_running and sys.platform == "win32":
            tray = Tray(window, url, conf["title"], on_quit=window.destroy)
            _STATE["tray"] = tray

            def on_closing():
                """关窗 = 收到托盘；只有托盘「退出」才真正结束。"""
                if _STATE["quitting"]:
                    return True
                tray.hide_to_tray()
                return False

            window.events.closing += on_closing
            webview.start(tray.start, gui=gui)

        elif keep_running and sys.platform == "darwin":
            menus = mac_menu(window, url)

            def on_closing_mac():
                """mac 没有托盘：关窗改为隐藏，进程留着继续跑后端。"""
                if _STATE["quitting"]:
                    return True
                try:
                    window.hide()
                except Exception as exc:
                    print(f"[warn] 隐藏窗口失败：{exc}")
                return False

            window.events.closing += on_closing_mac
            webview.start(gui=gui, menu=menus)

        else:
            webview.start(gui=gui)
    finally:
        if tray is not None:
            tray.stop()
        stop_backend(proc)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
