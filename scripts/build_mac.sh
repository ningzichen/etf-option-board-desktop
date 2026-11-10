#!/usr/bin/env bash
# 在 macOS 上打出可双击的 .app（arm64 / x64 按当前机器决定）。
#
#   bash scripts/build_mac.sh                  # 用主项目 master
#   REF=feat/data-dir bash scripts/build_mac.sh   # 用某个分支（ETF_DATA_DIR 还没合并时用这个）
#
# 产物：dist/EtfBoard.app
set -euo pipefail

cd "$(dirname "$0")/.."
REF="${REF:-master}"

if [ ! -x .venv/bin/python ]; then
  echo "== 建虚拟环境"
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt

echo "== 取主项目代码（ref=$REF）"
.venv/bin/python scripts/prepare_core.py --ref "$REF"

echo "== 打包"
rm -rf build/EtfBoard dist/EtfBoard.app
.venv/bin/python -m PyInstaller packaging/etf_board.spec --noconfirm

# 不签名的话 Gatekeeper 会直接拦（"已损坏，无法打开"）。ad-hoc 签名能让它至少
# 走"右键 → 打开"的路径；要彻底消掉提示得用 Developer ID，见 README。
echo "== ad-hoc 签名"
codesign --force --deep --sign - "dist/EtfBoard.app" || \
  echo "[warn] ad-hoc 签名失败，用户需手动在「系统设置 → 隐私与安全性」里放行"

echo "== 完成：dist/EtfBoard.app"
du -sh "dist/EtfBoard.app"
