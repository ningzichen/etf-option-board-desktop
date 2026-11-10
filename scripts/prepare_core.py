"""把主项目按固定 ref 取到 build/core，只拷运行需要的部分。

为什么不是直接引用主项目目录：
  打包产物必须自带主项目代码（用户机器上没有这个仓库），而桌面壳仓库不该
  复制一份源码进 git（三个月后必然和上游脱节）。所以：**构建时按 commit 取**，
  build/ 目录整个不进版本控制。

用法：
    python scripts/prepare_core.py                        # 取上游 master
    python scripts/prepare_core.py --ref 1da16cb          # 固定到某个 commit/tag
    python scripts/prepare_core.py --local D:\\GitHub\\etf-option-board   # 用本地仓库调试
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE_SRC = ROOT / "build" / "_core_src"
CORE_DST = ROOT / "build" / "core"

# 只拷 app 运行真正需要的：前端产物、服务层、路由、入口与配置
WHITELIST = ("app.py", "config.py", "paths.py", "services", "routers", "web/dist")
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules", ".pytest_cache")


def utf8_stdio() -> None:
    """Windows 的 CI 和控制台默认 cp1252/GBK，print 中文直接 UnicodeEncodeError。

    （GitHub 的 windows runner 就是这么挂的，别问为什么。）
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def run(*cmd: str, cwd: Path | None = None) -> str:
    print("$", " ".join(cmd))
    out = subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)
    return (out.stdout or "").strip()


def sync_source(repo: str, ref: str) -> tuple[Path, str]:
    """拉取主项目源码，返回 (目录, 实际 commit)。"""
    if (CORE_SRC / ".git").is_dir():
        run("git", "fetch", "--depth", "1", "origin", ref, cwd=CORE_SRC)
        run("git", "checkout", "--force", "--detach", "FETCH_HEAD", cwd=CORE_SRC)
        run("git", "clean", "-xdfq", cwd=CORE_SRC)
    else:
        CORE_SRC.parent.mkdir(parents=True, exist_ok=True)
        if CORE_SRC.exists():
            shutil.rmtree(CORE_SRC)
        run("git", "clone", "--depth", "1", "--branch", ref, repo, str(CORE_SRC))
    return CORE_SRC, run("git", "rev-parse", "HEAD", cwd=CORE_SRC)


def build_web(src: Path) -> None:
    """在主项目源码里构建前端。

    主项目的 web/dist 是 gitignore 的，仓库里没有现成产物；CI 上必须现建，
    否则打出来的包打开就是白屏。
    """
    web = src / "web"
    if not (web / "package.json").is_file():
        sys.exit(f"主项目缺少 {web}/package.json，没法构建前端")
    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    if (web / "package-lock.json").is_file():
        run(npm, "ci", cwd=web)
    else:
        run(npm, "install", cwd=web)
    run(npm, "run", "build", cwd=web)


def stage(src: Path, commit: str) -> None:
    """按白名单拷到 build/core（PyInstaller 会把它当数据目录打进去）。"""
    if CORE_DST.exists():
        shutil.rmtree(CORE_DST)
    CORE_DST.mkdir(parents=True)
    for name in WHITELIST:
        s = src / name
        if not s.exists():
            print(f"[warn] 主项目缺少 {name}，已跳过")
            continue
        t = CORE_DST / name
        if s.is_dir():
            shutil.copytree(s, t, ignore=IGNORE)
        else:
            shutil.copy2(s, t)
    (CORE_DST / "CORE_COMMIT").write_text(commit + "\n", encoding="utf-8")
    print(f"[ok] 已准备 {CORE_DST}（基于 {commit[:7]}）")


def main() -> None:
    utf8_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="https://github.com/NIO-SAI/etf-option-board")
    ap.add_argument("--ref", default="master")
    ap.add_argument("--local", default="", help="用本地仓库代替 clone（调试用，不拷 .git）")
    ap.add_argument("--build-web", action="store_true",
                    help="先构建前端再打包（CI 必须加：web/dist 不在仓库里）")
    args = ap.parse_args()

    if args.local:
        src = Path(args.local).resolve()
        if not (src / "app.py").is_file():
            sys.exit(f"不是主项目目录：{src}")
        commit = run("git", "rev-parse", "HEAD", cwd=src) if (src / ".git").is_dir() else "local"
    else:
        src, commit = sync_source(args.repo, args.ref)

    if args.build_web:
        build_web(src)

    stage(src, commit)

    # 硬依赖：没有 paths.py 就没有 ETF_DATA_DIR，打包版会把数据往安装目录里写
    # （Program Files / 签名过的 .app 都只读），装到别人机器上必定崩。
    if not (CORE_DST / "paths.py").is_file():
        sys.exit(
            "主项目这个 ref 里没有 paths.py（ETF_DATA_DIR 支持，见主项目 PR #17）。\n"
            "换一个已合并的分支：python scripts/prepare_core.py --ref master\n"
            "或在合并前临时用：--ref feat/data-dir"
        )

    if not (CORE_DST / "web" / "dist" / "index.html").is_file():
        sys.exit(
            "主项目没有构建好的 web/dist，打出来会白屏。\n"
            "加 --build-web 现建（需要 node/npm），或先在主项目里跑 web/build.sh"
        )


if __name__ == "__main__":
    main()
