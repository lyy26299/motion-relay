#!/usr/bin/env python3
"""
测试 Vision Agent 环境配置
运行此脚本检查所有依赖和 API 密钥是否正确配置
"""
import os
import sys
from importlib.util import find_spec
from pathlib import Path


def print_header(text):
    print("\n" + "=" * 60)
    print(f"  {text}")
    print("=" * 60)


def print_check(name, status, detail=""):
    icon = "✓" if status else "✗"
    status_text = "通过" if status else "失败"
    print(f"{icon} {name}: {status_text}")
    if detail:
        print(f"  → {detail}")


def check_python_version():
    print_header("检查 Python 版本")
    version = sys.version_info
    required = (3, 13)

    current = f"{version.major}.{version.minor}.{version.micro}"
    is_ok = required <= version < (3, 14)
    print_check(
        "Python 版本",
        is_ok,
        f"当前: {current}, 要求: >=3.13,<3.14"
    )
    return is_ok


def check_dependencies():
    print_header("检查 Python 依赖包")

    checks = []

    # 检查 vision_agents
    try:
        import vision_agents
        print_check("vision-agents", True, f"版本: {getattr(vision_agents, '__version__', '未知')}")
        checks.append(True)
    except ImportError as e:
        print_check("vision-agents", False, f"未安装: {e}")
        checks.append(False)

    # 检查 dotenv
    try:
        available = find_spec("dotenv") is not None
        if not available:
            raise ImportError
        print_check("python-dotenv", True)
        checks.append(True)
    except ImportError:
        print_check("python-dotenv", False, "未安装")
        checks.append(False)

    # 检查 ultralytics
    try:
        import ultralytics
        print_check("ultralytics (YOLO)", True, f"版本: {ultralytics.__version__}")
        checks.append(True)
    except ImportError:
        print_check("ultralytics", False, "未安装")
        checks.append(False)

    return all(checks)


def check_env_file():
    print_header("检查环境变量配置")

    env_path = Path(".env")
    if not env_path.exists():
        print_check(".env 文件", False, "文件不存在，请复制 .env.example")
        return False

    print_check(".env 文件", True, "文件存在")

    # 加载环境变量
    from dotenv import load_dotenv
    load_dotenv()

    checks = []

    # The local desktop entry point uses Qwen Realtime through DashScope.
    dashscope_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    configured = bool(dashscope_key and dashscope_key != "your_dashscope_api_key_here")
    print_check(
        "DASHSCOPE_API_KEY",
        configured,
        "已配置（值未显示）" if configured else "未配置或使用默认值",
    )
    checks.append(configured)

    return all(checks)


def check_yolo_model():
    print_header("检查 YOLO 模型")

    model_path = Path("yolo11n-pose.pt")

    if model_path.exists():
        size_mb = model_path.stat().st_size / (1024 * 1024)
        print_check("yolo11n-pose.pt", True, f"已下载 ({size_mb:.1f} MB)")
        return True
    else:
        print_check("yolo11n-pose.pt", False, "首次运行时会自动下载")
        return True  # 不算失败，因为会自动下载


def check_instructions():
    print_header("检查指令文件")

    instructions_path = Path("docs/COACHING_INSTRUCTIONS.md")

    if instructions_path.exists():
        size_kb = instructions_path.stat().st_size / 1024
        print_check("docs/COACHING_INSTRUCTIONS.md", True, f"存在 ({size_kb:.1f} KB)")
        return True
    else:
        print_check("docs/COACHING_INSTRUCTIONS.md", False, "指令文件缺失")
        return False


def print_summary(results):
    print_header("测试总结")

    total = len(results)
    passed = sum(results.values())
    failed = total - passed

    print(f"\n总计: {total} 项检查")
    print(f"✓ 通过: {passed}")
    print(f"✗ 失败: {failed}")

    if all(results.values()):
        print("\n🎉 所有检查通过！你可以运行 ./run.sh 启动本地教练")
        print("\n快速启动:")
        print("  ./run.sh              # macOS/Linux")
        print("  run.bat               # Windows")
        print("  .venv/bin/python agent_local.py")
        return 0
    else:
        print("\n⚠ 部分检查失败，请根据上述提示修复问题")
        print("\n常见解决方案:")

        if not results.get("python"):
            print("- Python 版本: 使用 Python 3.13（不支持 3.14+）")
        if not results.get("deps"):
            print("- 依赖包: 运行 uv sync --locked")
        if not results.get("env"):
            print("- 环境变量: 编辑 .env 文件并填入 DASHSCOPE_API_KEY")
        if not results.get("instructions"):
            print("- 指令文件: 确保 docs/COACHING_INSTRUCTIONS.md 存在")

        print("\n获取 API 密钥: https://bailian.console.aliyun.com/")

        return 1


def main():
    print_header("Vision Agent 环境配置测试")
    print("此脚本将检查所有必需的依赖和配置")

    results = {}

    # 运行所有检查
    results["python"] = check_python_version()
    results["deps"] = check_dependencies()
    results["env"] = check_env_file()
    results["yolo"] = check_yolo_model()
    results["instructions"] = check_instructions()

    # 输出总结
    exit_code = print_summary(results)

    print("\n" + "=" * 60)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
