#!/usr/bin/env bash
# 修补 forexconnect PyPI 轮子在 macOS 上硬编码的库路径，使其兼容 uv 托管的 Python 3.10。
#
# 背景：forexconnect==1.6.43 的 cp310-macosx_11_0_arm64 轮子由 python.org 官方框架版
# Python 构建，扩展与部分 dylib 里有两类绝对路径依赖：
#   1) /Library/Frameworks/Python.framework/Versions/3.10/Python  -> uv Python 的 libpython
#   2) /usr/local/lib/libz.1.dylib（旧 x86 Homebrew 路径，Apple Silicon 上不存在）-> 系统 zlib
#
# 用法：
#   bash scripts/patch_forexconnect_mac.sh            # 自动定位 libpython
#   bash scripts/patch_forexconnect_mac.sh <libpython路径>  # 手动指定
#
# 重建 venv / 重装 forexconnect 后需重新执行本脚本。
set -euo pipefail

cd "$(dirname "$0")/.."

LIB_DIR=".venv/lib/python3.10/site-packages/forexconnect/lib"
[ -d "$LIB_DIR" ] || { echo "错误：未找到 $LIB_DIR，请先安装 forexconnect"; exit 1; }

# 定位 libpython：优先 Homebrew 框架版（动态链接 libpython 的解释器）。
# 注意：uv 托管的 python-build-standalone 主执行档是静态链接的，扩展会绑定到第二份
# 未初始化的 libpython 导致段错误（双运行时问题），因此必须用 brew/官方框架版 Python。
BREW_PY="/opt/homebrew/opt/python@3.10/Frameworks/Python.framework/Versions/3.10/Python"
if [ -n "${1:-}" ]; then
  LIBPYTHON="$1"
elif [ -f "$BREW_PY" ]; then
  LIBPYTHON="$BREW_PY"
else
  LIBPYTHON="$(ls "$HOME"/.local/share/uv/python/cpython-3.10.*-macos-aarch64-none/lib/libpython3.10.dylib 2>/dev/null | head -n 1 || true)"
  if [ -z "$LIBPYTHON" ]; then
    echo "错误：未找到 libpython3.10.dylib。请先: brew install python@3.10"
    exit 1
  fi
  echo "警告：使用 uv 静态版 Python 的 libpython 可能导致段错误，建议改用 brew python@3.10"
fi

OLD_PY_FRAMEWORK="/Library/Frameworks/Python.framework/Versions/3.10/Python"
OLD_LIBZ="/usr/local/lib/libz.1.dylib"
NEW_LIBZ="/usr/lib/libz.1.dylib"

patched=0
for f in "$LIB_DIR"/*.so "$LIB_DIR"/*.dylib; do
  [ -f "$f" ] || continue
  changed=0
  if otool -L "$f" | grep -qF "$OLD_PY_FRAMEWORK"; then
    install_name_tool -change "$OLD_PY_FRAMEWORK" "$LIBPYTHON" "$f"
    echo "已修补 libpython 引用: $(basename "$f")"
    changed=1
  fi
  if otool -L "$f" | grep -qF "$OLD_LIBZ" && [ ! -f "$OLD_LIBZ" ]; then
    install_name_tool -change "$OLD_LIBZ" "$NEW_LIBZ" "$f"
    echo "已修补 libz 引用: $(basename "$f")"
    changed=1
  fi
  if [ "$changed" -eq 1 ]; then
    codesign --force --sign - "$f"   # 修改后需重新 ad-hoc 签名（Apple Silicon 强制）
    patched=$((patched + 1))
  fi
done

echo "共修补 $patched 个二进制"
.venv/bin/python -c "import forexconnect; print('IMPORT OK:', forexconnect.__file__)"
