#!/usr/bin/env python3
"""Check the local Qwen Vision Coach environment without exposing secrets."""

from __future__ import annotations

import os
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VISION_AGENTS = "0.6.9"


def print_header(text: str) -> None:
    print("\n" + "=" * 60)
    print(f"  {text}")
    print("=" * 60)


def print_check(name: str, status: bool, detail: str = "") -> None:
    icon = "✓" if status else "✗"
    status_text = "通过" if status else "失败"
    print(f"{icon} {name}: {status_text}")
    if detail:
        print(f"  → {detail}")


def check_python_version() -> bool:
    print_header("检查 Python 版本")
    # pyproject.toml pins this dependency set to Python 3.13; 3.14 is not
    # interchangeable because native media wheels are resolved per minor.
    required = (3, 13)
    maximum = (3, 14)
    current = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    is_ok = required <= sys.version_info < maximum
    print_check("Python 版本", is_ok, f"当前: {current}, 要求: >=3.13,<3.14")
    return is_ok


def check_dependencies() -> bool:
    print_header("检查 Python 依赖包")
    checks: list[bool] = []
    for distribution, label in (
        ("vision-agents", "vision-agents"),
        ("python-dotenv", "python-dotenv"),
        ("ultralytics", "ultralytics (YOLO)"),
        ("websockets", "websockets"),
    ):
        try:
            installed = version(distribution)
            matches_contract = (
                distribution != "vision-agents" or installed == EXPECTED_VISION_AGENTS
            )
            detail = f"版本: {installed}"
            if not matches_contract:
                detail += f"，方案 A 当前锁定 {EXPECTED_VISION_AGENTS}"
            print_check(label, matches_contract, detail)
            checks.append(matches_contract)
        except PackageNotFoundError:
            print_check(label, False, "未安装")
            checks.append(False)
    return all(checks)


def check_env_file() -> bool:
    print_header("检查环境变量配置")
    env_path = ROOT / ".env"
    if not env_path.exists():
        print_check(".env 文件", False, "文件不存在，请复制 .env.example")
        return False

    print_check(".env 文件", True, "文件存在")
    from dotenv import load_dotenv

    load_dotenv(env_path)
    dashscope_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    configured = bool(dashscope_key and dashscope_key != "your_dashscope_api_key_here")
    print_check(
        "DASHSCOPE_API_KEY",
        configured,
        "已配置（值未显示）" if configured else "未配置或仍为占位值",
    )
    return configured


def check_yolo_model() -> bool:
    print_header("检查 YOLO 模型")
    model_path = ROOT / "yolo11n-pose.pt"
    if model_path.exists():
        size_mb = model_path.stat().st_size / (1024 * 1024)
        print_check("yolo11n-pose.pt", True, f"已下载 ({size_mb:.1f} MB)")
    else:
        print_check("yolo11n-pose.pt", True, "未预下载；首次运行会自动下载")
    return True


def check_instructions() -> bool:
    print_header("检查指令文件")
    instructions_path = ROOT / "docs" / "COACHING_INSTRUCTIONS.md"
    if instructions_path.is_file():
        size_kb = instructions_path.stat().st_size / 1024
        print_check("docs/COACHING_INSTRUCTIONS.md", True, f"存在 ({size_kb:.1f} KB)")
        return True
    print_check("docs/COACHING_INSTRUCTIONS.md", False, "指令文件缺失")
    return False


def print_summary(results: dict[str, bool]) -> int:
    print_header("测试总结")
    passed = sum(results.values())
    failed = len(results) - passed
    print(f"\n总计: {len(results)} 项检查")
    print(f"✓ 通过: {passed}")
    print(f"✗ 失败: {failed}")
    if all(results.values()):
        print("\n所有检查通过，可以运行：")
        print("  .venv/bin/python agent_local.py")
        return 0

    print("\n部分检查失败，请根据上述提示修复：")
    if not results.get("python"):
        print("- Python 版本：使用 Python 3.13（不支持 3.14+）")
    if not results.get("deps"):
        print("- 依赖包：运行 uv sync --locked")
    if not results.get("env"):
        print("- 环境变量：在 .env 中配置 DASHSCOPE_API_KEY")
    if not results.get("instructions"):
        print("- 指令文件：确保 docs/COACHING_INSTRUCTIONS.md 存在")
    return 1


def main() -> None:
    print_header("本地 Vision Coach 环境配置测试")
    results = {
        "python": check_python_version(),
        "deps": check_dependencies(),
        "env": check_env_file(),
        "yolo": check_yolo_model(),
        "instructions": check_instructions(),
    }
    raise SystemExit(print_summary(results))


if __name__ == "__main__":
    main()
