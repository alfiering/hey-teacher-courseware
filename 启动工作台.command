#!/bin/bash
# 课件超级工作台 · 每次使用时双击。这个窗口开着，工作台就在运行；关掉窗口就停止。
cd "$(dirname "$0")" || exit 1

pause_and_exit() {
  echo
  read -r -p "按回车关闭这个窗口…" _
  exit "$1"
}

if [ ! -x .venv/bin/python ]; then
  echo "还没有安装。请先双击「安装依赖.command」。"
  pause_and_exit 1
fi
PORT=8792
URL="http://127.0.0.1:$PORT/"
if curl -fsS --max-time 2 "${URL}api/ping" 2>/dev/null | .venv/bin/python -c 'import json,sys;sys.exit(0 if json.load(sys.stdin).get("app")=="allsubject-local-workbench" else 1)' 2>/dev/null; then
  echo "工作台已经在运行，正在打开浏览器。"
  open "$URL"
  exit 0
fi
if lsof -n -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "端口 $PORT 被其他程序占用，工作台没有启动（也没有关闭那个程序）。"
  echo "可以重启电脑后再试；还不行请把这个窗口截图发给作者。"
  pause_and_exit 1
fi
.venv/bin/python -u web/local_server.py "$PORT" &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT
for _ in $(seq 1 75); do
  if curl -fsS --max-time 1 "${URL}api/ping" >/dev/null 2>&1; then
    open "$URL"
    echo
    echo "✓ 工作台已打开：$URL"
    echo "  使用期间请不要关闭这个窗口；用完直接关掉即可。"
    echo "  课程资料保存在本文件夹的 data/local_workbench 里。"
    wait "$SERVER_PID"
    exit $?
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "✗ 工作台启动失败。请把这个窗口截图发给作者。"
    pause_and_exit 1
  fi
  sleep 0.2
done
echo "✗ 工作台没有在预期时间内启动。请把这个窗口截图发给作者。"
pause_and_exit 1
