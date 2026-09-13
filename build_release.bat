@echo off
REM ============================================================
REM  图寻助手 v2.0 - 单文件 Release 构建脚本
REM  产物在 dist\ 目录：
REM    TuxunHelper-Realtime.exe  后台服务（网页前端 + 双镜像 + TUI + 悬浮窗）
REM    TuxunHelper-AI.exe        AI 分析 / 复盘 / 抽奖（命令行）
REM  2.0 起为全网页版：无 pywebview / 无原生窗口，web\ 目录
REM  （index.html 前端 + vendor Leaflet + tutorial.md 教程）打包进 exe
REM  首次运行会自动创建 .build-venv 构建环境
REM ============================================================
setlocal
cd /d %~dp0

if not exist .build-venv\Scripts\python.exe (
    echo [构建] 首次运行，创建构建环境（约 2-5 分钟）...
    python -m venv .build-venv
    .build-venv\Scripts\python -m pip install -q -U pip -i https://pypi.tuna.tsinghua.edu.cn/simple
    .build-venv\Scripts\pip install -q requests python-dotenv pillow "mitmproxy>=10" "bcrypt==4.0.1" "rich>=13" pyinstaller websocket-client -i https://pypi.tuna.tsinghua.edu.cn/simple
)

echo [构建] 后台服务 TuxunHelper-Realtime.exe ...
.build-venv\Scripts\pyinstaller --noconfirm --onefile --console --clean ^
  --name TuxunHelper-Realtime ^
  --add-data "web;web" ^
  --collect-all mitmproxy ^
  --collect-submodules mitmproxy ^
  --collect-all mitmproxy_rs ^
  --collect-data kaitaistruct ^
  --collect-data publicsuffix2 ^
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
