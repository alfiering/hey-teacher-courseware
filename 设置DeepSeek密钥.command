#!/bin/bash
# 课件超级工作台 · 填写或更换 DeepSeek API key。key 只保存在本文件夹的 config.local.json，不会上传。
cd "$(dirname "$0")" || exit 1

echo "==== 设置 DeepSeek API key ===="
echo "生成教案和逐页提示词需要它。没有的话，到 https://platform.deepseek.com 注册并创建 API key（按用量付费，每节课通常几分钱到几毛钱）。"
echo
read -r -s -p "请粘贴 key（粘贴时屏幕不显示，粘好直接回车；不填直接回车跳过）：" KEY
echo
KEY="$(printf '%s' "$KEY" | tr -d '[:space:]')"
if [ -z "$KEY" ]; then
  echo "已跳过。以后需要时再双击「设置DeepSeek密钥.command」。"
else
  PY=.venv/bin/python
  [ -x "$PY" ] || PY=python3
  if DEEPSEEK_KEY="$KEY" "$PY" - <<'PY'
import json, os
from pathlib import Path
path = Path('config.local.json')
try:
    cfg = json.loads(path.read_text(encoding='utf-8'))
except (OSError, ValueError):
    cfg = {}
cfg['deepseek_key'] = os.environ['DEEPSEEK_KEY']
path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
os.chmod(path, 0o600)
PY
  then
    echo "✓ 已保存。如果工作台正在运行，请关掉它的窗口，再双击「启动工作台.command」重新打开。"
  else
    echo "✗ 保存失败。请把这个窗口截图发给作者。"
  fi
fi
[ -n "${WB_FROM_INSTALL:-}" ] && exit 0
echo
read -r -p "按回车关闭这个窗口…" _
