#!/bin/bash
# 课件超级工作台 · 第一次使用前双击一次：检查电脑、准备 Python 环境、安装依赖。
cd "$(dirname "$0")" || exit 1

pause_and_exit() {
  echo
  read -r -p "按回车关闭这个窗口…" _
  exit "$1"
}

echo "==== 课件超级工作台 · 安装 ===="
echo

if [ "$(uname -s)" != "Darwin" ]; then
  echo "✗ 这个版本只支持 Mac 电脑。"
  pause_and_exit 1
fi

# 1. 找一个 3.11 或更新的 Python（Mac 自带的 python3 通常是 3.9，不能用）
PY=""
for candidate in python3.13 python3.12 python3.11 \
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
    /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
      "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    PY="$(command -v "$candidate")"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "✗ 没有找到 Python 3.11 或更新版本。"
  echo
  echo "  请这样做："
  echo "  1. 在马上打开的网页里，下载「macOS 64-bit universal2 installer」（3.12 或 3.13 都可以）"
  echo "  2. 双击下载好的 .pkg，一路点「继续」装完"
  echo "  3. 回到这里，再双击一次「安装依赖.command」"
  open "https://www.python.org/downloads/macos/"
  pause_and_exit 1
fi
echo "✓ Python：$("$PY" --version 2>&1)"

# 2. 在本文件夹里建立独立环境，不影响电脑上的其他软件
if [ ! -x .venv/bin/python ]; then
  echo "… 正在建立工作台专用的 Python 环境"
  if ! "$PY" -m venv .venv; then
    echo "✗ 建立环境失败。请把这个窗口截图发给作者。"
    pause_and_exit 1
  fi
fi

# 3. 安装依赖：先用国内的清华镜像（快），失败再用官方源
echo "… 正在下载并安装依赖（约 200 MB，第一次大约 5–20 分钟，取决于网速）"
install_deps() {
  .venv/bin/python -m pip install --disable-pip-version-check --progress-bar on --timeout 60 "$@" -r requirements-community.txt
}
if ! install_deps -i https://pypi.tuna.tsinghua.edu.cn/simple; then
  echo
  echo "… 镜像下载失败，改用官方源重试"
  if ! install_deps; then
    echo
    echo "✗ 依赖安装失败。请检查网络后再双击一次；还不行就把这个窗口截图发给作者。"
    pause_and_exit 1
  fi
fi
if ! .venv/bin/python -c 'import pymupdf, docx, pptx, PIL, reportlab, playwright' >/dev/null 2>&1; then
  echo "✗ 依赖装完但无法加载。请把这个窗口截图发给作者。"
  pause_and_exit 1
fi
echo "✓ 依赖已安装"

# 4. 检查另外两个软件（不影响启动，缺了只是部分功能用不了）
echo
if [ -d "/Applications/Google Chrome.app" ]; then
  echo "✓ Google Chrome 已安装"
else
  echo "△ 没找到 Google Chrome：自动操作 Gemini Notebook 需要它。下载：https://www.google.com/chrome/"
fi
if [ -x "/Applications/LibreOffice.app/Contents/MacOS/soffice" ] || command -v soffice >/dev/null 2>&1; then
  echo "✓ LibreOffice 已安装"
else
  echo "△ 没找到 LibreOffice：检查 PPTX 课件、生成逐页预览需要它。下载：https://www.libreoffice.org/download/download/"
fi

if ! grep -q '"deepseek_key"' config.local.json 2>/dev/null; then
  echo
  echo "还差一步：填写 DeepSeek API key（生成教案和提示词要用）。"
  WB_FROM_INSTALL=1 bash ./设置DeepSeek密钥.command
fi

echo
echo "==== 安装完成 ===="
echo "以后每次使用，双击「启动工作台.command」即可。"
pause_and_exit 0
