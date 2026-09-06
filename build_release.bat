@echo off
REM ============================================================
REM  图寻助手 - 单文件 Release 构建脚本
REM  产物在 dist\ 目录：
REM    TuxunHelper-Realtime.exe  实时取点（GUI + 本地代理）
REM    TuxunHelper-AI.exe        AI 分析 / 复盘 / 抽奖（命令行）
REM  首次运行会自动创建 .build-venv 构建环境
REM ============================================================
setlocal
cd /d %~dp0

if not exist .build-venv\Scripts\python.exe (
    echo [构建] 首次运行，创建构建环境（约 2-5 分钟）...
    python -m venv .build-venv
    .build-venv\Scripts\python -m pip install -q -U pip -i https://pypi.tuna.tsinghua.edu.cn/simple
    .build-venv\Scripts\pip install -q requests python-dotenv pillow "mitmproxy>=10" "pywebview>=5.4,<6" "bcrypt==4.0.1" pyinstaller websocket-client -i https://pypi.tuna.tsinghua.edu.cn/simple
)

set PIPINF=--no-warn-script-location

echo [构建] 实时取点 TuxunHelper-Realtime.exe ...
.build-venv\Scripts\pyinstaller --noconfirm --onefile --console --clean ^
  --name TuxunHelper-Realtime ^
  --add-data "gui.html;." ^
  --collect-all mitmproxy ^
  --collect-submodules mitmproxy ^
  --collect-all mitmproxy_rs ^
  --collect-data kaitaistruct ^
  --collect-data publicsuffix2 ^
  --collect-submodules webview ^
  --hidden-import webview.platforms.winforms ^
  --hidden-import webview.platforms.edgechromium ^
  --collect-all clr_loader ^
  --collect-all pythonnet ^
  tuxun_proxy.py || goto :err

echo [构建] AI 分析 TuxunHelper-AI.exe ...
.build-venv\Scripts\pyinstaller --noconfirm --onefile --console --clean ^
  --name TuxunHelper-AI ^
  --hidden-import PIL._tkinter_finder ^
  main.py || goto :err

echo.
echo [完成] 产物在 dist\ 目录
exit /b 0

:err
echo [失败] 构建出错，请检查上方日志
exit /b 1
