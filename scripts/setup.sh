#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "=========================================="
echo "  Vision Coach 本地训练台 - 安装"
echo "=========================================="

if ! command -v uv >/dev/null 2>&1; then
    echo "错误: 未找到 uv。请先安装: https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
fi

echo "安装 Python 3.13 依赖..."
uv sync --locked

if [[ ! -f .env ]]; then
    cp .env.example .env
    echo "已创建 .env；请编辑该文件并填写 DASHSCOPE_API_KEY。"
else
    echo "✓ .env 文件已存在"
fi

if [[ -f yolo11n-pose.pt ]]; then
    echo "✓ yolo11n-pose.pt 已存在"
else
    echo "提示: 首次启动会由 Ultralytics 自动下载 yolo11n-pose.pt（约 6 MB）。"
fi

echo ""
echo "检查本地配置（不会打印密钥）..."
if ! "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/check_local_setup.py"; then
    echo "请完成 .env 配置后再次运行该检查。"
fi

echo ""
echo "安装完成。启动命令:"
echo "  ./run.sh"
echo "  .venv/bin/python agent_local.py"
