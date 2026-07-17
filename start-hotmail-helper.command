#!/bin/bash
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

DEFAULT_HOST="${HOTMAIL_HELPER_HOST:-127.0.0.1}"
DEFAULT_PORT="${HOTMAIL_HELPER_PORT:-17373}"
PYTHON_BIN="${PYTHON_BIN:-}"
HELPER_SCRIPT="$SCRIPT_DIR/scripts/hotmail_helper.py"

if [ ! -f "$HELPER_SCRIPT" ]; then
  echo "找不到 helper 脚本：$HELPER_SCRIPT"
  echo "请从 GuJumpgate 项目根目录启动 start-hotmail-helper.command。"
  read -r -p "按回车退出..."
  exit 1
fi

if [ -z "$PYTHON_BIN" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
  fi
fi

if [ -z "$PYTHON_BIN" ] || ! "$PYTHON_BIN" --version >/dev/null 2>&1; then
  echo "未找到可用 Python。请先安装 Python 3.10+，或设置 PYTHON_BIN。"
  echo "例如：PYTHON_BIN=/opt/homebrew/bin/python3 ./start-hotmail-helper.command"
  read -r -p "按回车退出..."
  exit 1
fi

HOST="$DEFAULT_HOST"
PORT="$DEFAULT_PORT"
EXTRA_ARGS=()

if [ "$#" -eq 1 ] && [[ "$1" =~ ^[0-9]+$ ]]; then
  PORT="$1"
else
  EXTRA_ARGS=("$@")
fi

echo "============================================================"
echo "GuJumpgate Outlook/Hotmail 本地接码 helper"
echo "项目目录：$SCRIPT_DIR"
echo "Python：$PYTHON_BIN"
echo "监听地址：http://$HOST:$PORT"
echo "健康检查：http://$HOST:$PORT/health"
echo "============================================================"
echo
echo "启动后不要关闭这个窗口；扩展侧边栏里的“本地助手”地址填："
echo "http://$HOST:$PORT"
echo

mkdir -p "$SCRIPT_DIR/data"

if [ "${#EXTRA_ARGS[@]}" -gt 0 ]; then
  "$PYTHON_BIN" -u "$HELPER_SCRIPT" --host "$HOST" --port "$PORT" "${EXTRA_ARGS[@]}"
else
  "$PYTHON_BIN" -u "$HELPER_SCRIPT" --host "$HOST" --port "$PORT"
fi
EXIT_CODE=$?

echo
echo "helper 已退出，退出码：$EXIT_CODE"
read -r -p "按回车关闭窗口..."
exit "$EXIT_CODE"
