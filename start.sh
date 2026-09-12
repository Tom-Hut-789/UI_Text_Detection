#!/usr/bin/env bash
# ============================================================
#  多语种UI文本格式检测系统 —— 一键启动（macOS / Linux）
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

PY=$(command -v python3 || command -v python || true)
if [ -z "$PY" ]; then
  echo "[错误] 未检测到 python3，请先安装 Python 3.10+" >&2
  exit 1
fi

exec "$PY" start.py "$@"
