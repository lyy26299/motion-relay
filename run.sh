#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$ROOT_DIR/.venv/bin/python"

echo "=========================================="
echo "  启动 Vision Coach 本地实时训练台"
echo "=========================================="

if [[ ! -x "$PYTHON" ]]; then
    echo "错误: 未找到项目虚拟环境: $PYTHON" >&2
    echo "请先运行: uv sync --locked" >&2
    exit 1
fi

if ! "$PYTHON" "$ROOT_DIR/scripts/check_local_setup.py"; then
    echo "配置检查未通过。请复制 .env.example 为 .env，并填写 DASHSCOPE_API_KEY。" >&2
    exit 1
fi

echo "启动桌面训练台（Qwen Realtime + YOLO Pose）..."
cd "$ROOT_DIR"
exec "$PYTHON" "$ROOT_DIR/agent_local.py"
