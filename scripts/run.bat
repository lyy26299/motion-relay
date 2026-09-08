@echo off
setlocal
cd /d "%~dp0\.."
set "PYTHON=%CD%\.venv\Scripts\python.exe"

echo ==========================================
echo   启动 Vision Coach 本地实时训练台
echo ==========================================

if not exist "%PYTHON%" (
    echo 错误: 未找到项目虚拟环境: %PYTHON%
    echo 请先运行: uv sync --locked
    exit /b 1
)

"%PYTHON%" scripts\check_local_setup.py
if errorlevel 1 (
    echo 配置检查未通过。请复制 .env.example 为 .env，并填写 DASHSCOPE_API_KEY。
    exit /b 1
)

echo 启动桌面训练台（Qwen Realtime + YOLO Pose）...
"%PYTHON%" agent_local.py
exit /b %errorlevel%
