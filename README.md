<div align="center">

# 🌍 图寻助手 (Tuxun Helper) v2.0 · 全网页版

**图寻 / GeoGuessr 双平台游戏辅助工具 —— 免证书、免代理，浏览器直接玩**

[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)]()
[![AI](https://img.shields.io/badge/AI-Gemini%20%7C%20GLM--4V%20%7C%20Qwen--VL-8A2BE2)]()

实时显示街景真实坐标 · 游戏内地图标记 · AI 街景分析 · 打完即复盘 · 国内网络直连可用

</div>

> [!WARNING]
> 本项目仅供学习和技术交流使用。请在遵守平台服务条款的前提下合理使用，因使用本工具产生的一切后果由使用者自行承担。

---

## 📌 这是什么？

[图寻](https://tuxun.fun) 是一款类似 GeoGuessr 的地理定位游戏：给你一段街景，猜它在世界/中国哪里，猜得越近分越高。

**图寻助手** 帮你做三件事：

1. **边玩边显示答案**：进入街景的瞬间，悬浮窗直接给出「原点（真实位置）」坐标和地址，还可在游戏地图上直接标出金色「原」点；积分赛/每日挑战倒计时结束，答案自动揭示。
2. **打完即复盘**：输入对局链接/ID，自动列出每一轮的真实坐标与地址，还能让 AI 同题作答对比误差。
3. **AI 分析定位**：给一张街景图，视觉大模型推断国家/地区/城市并给出坐标。

**核心优势：不需要装任何证书、不需要接管系统代理** —— 工具在本地开一个镜像网站（`http://127.0.0.1:8001`），用浏览器打开它就能正常做题，一切自动生效。

---

## ⚡ 新手 3 分钟上手（推荐路径）

> 以下是最简单的「镜像模式」：**免证书、免代理、不用命令行配环境变量**。装好 Python 后全程网页点几下即可。

### 第 1 步：安装 Python（若已安装可跳过）

到 [python.org](https://www.python.org/downloads/) 下载安装 **Python 3.9 或更高版本**。
Windows 安装时**务必勾选底部「Add Python to PATH」**。

### 第 2 步：下载本项目并安装依赖

```bash
git clone https://github.com/HighPing64x/tuxun-helper.git
cd tuxun-helper

# 国内用户建议用清华镜像加速
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

不想装 Python 的话，可以直接用现成的**单文件 exe**（见文末「📦 单文件打包」），双击即用。

### 第 3 步：启动

```bash
python tuxun_proxy.py --mirror --console
```

程序会自动打开选择页 **http://127.0.0.1:18080/**。此时你电脑上已经有：

| 地址 | 是什么 |
|---|---|
| `http://127.0.0.1:18080/` | 选择页：左=图寻 / 右=GeoGuessr |
| `http://127.0.0.1:8001` | 图寻镜像站（做题入口） |
| `http://127.0.0.1:8002` | GeoGuessr 镜像站（做题入口） |

### 第 4 步：登录（只需一次，Cookie 自动保存）

1. 在选择页点左侧「图寻」进入镜像站；
2. 在镜像站里正常登录（微信扫码等）；
3. 工具会**自动捕获并保存**你的登录 Cookie 到本地 `.env`，下次启动直接可用，无需重新登录。

> 也可以点选择页右下角圆环菜单的「登录」页手动粘贴 Cookie（浏览器 F12 → 网络 → 复制 `fun_ticket=` 开头的一段），同样自动保存。

### 第 5 步：开始做题

回到选择页 → 进入图寻镜像 → 玩每日挑战 / 积分赛 / 自定义对局。**游戏页面左下角的「📍 图寻助手」悬浮窗**会实时显示：

- **原点**：本回合街景的真实坐标 + 文字地址（金色）
- **目前**：你当前所在位置及距原点的距离（绿色，可移动回合）
- **答案**：倒计时结束自动揭示的答案坐标（蓝色）
- **干扰 / 候选**：可疑坐标计数（防诱饵）
- **游戏内地图标记**：金色「原」标在真实位置、蓝色「答」标在答案上，直接看地图不用盯面板

> 在悬浮窗里点 **⚙ 设置** 可调整各种开关，全部即时生效、无需重启。

---

## 🎮 日常使用详解

### 选择页（`http://127.0.0.1:18080/`）

深色分屏主页：左半边=图寻（橙色），右半边=GeoGuessr（蓝色），鼠标悬停有放大动效。**悬停中央白点**弹出圆环菜单：上=贡献者名单、左下=退出程序、右下=打开 GitHub。

### 游戏页悬浮窗

| 功能 | 说明 |
|---|---|
| 原点 / 目前 / 答案 | 实时坐标 + 地址（图寻与 GeoGuessr 都支持） |
| 游戏内地图标记 | 金色「原」=回合真值，蓝色「答」=答案，直接标在游戏地图上 |
| 一键特定分数 | ⚙ 设置里启用后，游戏内按热键（默认 `F9`）即以目标分数对应的距离自动落点提交，与手点完全同构 |
| 手机号归属地解析 | ⚙ 设置里粘贴中国 11 位手机号，自动取第 4~7 位号段解析为省市，辅助定位 |
| 设置抽屉 | 防诱饵 / API 直读 / 一键分数 / 名称保护等开关，即时生效 |

### 防诱饵（默认开启）

图寻的反作弊会向街景元数据里注入假坐标。工具用**双判定**免疫：距离变化 ≤150 米 = 同一地点；无移动回合的远距离坐标 = 诱饵排除；可移动回合的远距离坐标 = 玩家合法移动（静默）。另有 **API 直读**通道：被动读取浏览器自身的对局响应取干净真值，零额外请求。

---

## 🔧 进阶用法

### ① 拦截模式（在官网原站上玩，需要证书）

镜像模式是最省事的选择；只有当你**想直接在 tuxun.fun 官网原站上做题**时才需要它：

```bash
python tuxun_proxy.py --install-cert   # 一次性：安装根证书（需管理员）
python tuxun_proxy.py --proxy          # 启动并接管系统代理
```

之后正常访问 tuxun.fun 做题，坐标同样实时显示在网页仪表盘（`http://127.0.0.1:18080/` 的「地图/仪表盘」页）。退出程序自动还原你的代理设置，Clash 等代理软件零配置自动级联。

### ② AI 分析 / 复盘 / 抽奖（命令行）

```bash
python main.py                         # 交互菜单
python main.py <游戏ID或链接>           # 直接分析某局（自动识别平台）
python main.py --history               # 复盘最近 20 局（免 AI，秒出全轮真值）
python main.py --draw                  # 每日会员抽奖
```

AI 分析需要先在 `.env` 里配一个视觉模型 Key（见「配置参考」），国内模型（智谱 GLM-4V、通义 Qwen-VL）直连免代理，是默认推荐。

### ③ 后台仪表盘

```bash
python tuxun_proxy.py --tui --mirror   # 终端实时显示端口/捕获状态/日志
```

---

## ⚙️ 配置参考

### `.env`（AI 模式 / Cookie 存储，程序自动读写）

| 变量 | 用途 |
|---|---|
| `TUXUN_COOKIE` | 图寻凭证：`fun_ticket=...`、完整 Cookie 头、或浏览器插件导出的 JSON 文件路径（推荐） |
| `GEOGUESSR_COOKIE` | GeoGuessr 完整 Cookie（实验性） |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL` | 国内视觉模型（如智谱 `glm-4.5v`、通义 `qwen-vl-max`） |
| `API_KEY` / `GEMINI_MODEL` | Google Gemini（国内需配 `PROXY_URL`） |
| `PROXY_URL` | HTTP 代理，仅用于 Google 街景图 / Gemini（Clash 默认 `http://127.0.0.1:7890`） |
| `AI_PROVIDER` | 强制后端：`openai` / `gemini`（同时配两组时国内直连优先） |
| `AMAP_KEY` / `AMAP_JS_KEY` | 覆盖内置高德 Key（国内逆地址编码，已内置开箱即用） |

> `.env`、`cookies*.json`、`config.json`、`logs/` 均已被 `.gitignore` 忽略，不会提交到仓库。**Cookie 会过期**，失效时重新登录一次即可。

### `config.json`（运行配置，网页「设置」页可调，程序自动生成）

| 字段 | 说明 | 默认 |
|---|---|---|
| `mirror_enabled` | 启动自动开启镜像 | `true` |
| `mirror_port` | 图寻镜像端口 | `8001` |
| `mirror_port_geo` | GeoGuessr 镜像端口 | `8002` |
| `control_port` | 选择页 + 悬浮窗 API 端口 | `18080` |
| `overlay_enabled` | 是否向游戏页注入悬浮窗（关掉页面干净） | `true` |
| `api_poll` | API 直读取真值（被动、零额外请求） | `true` |
| `anti_decoy` | 防诱饵双判定 | `true` |
| `near_m` | 多少米内视为同一地点 | `150` |
| `map_tiles` | 仪表盘瓦片源：`amap`（高德，默认）/ `osm` / `arcgis` | `amap` |
| `oneclock_enabled` / `oneclock_score` / `oneclock_key` | 一键特定分数：开关 / 目标分（200-4990）/ 热键 | `false` / `3500` / `F9` |
| `map_size` | 一键分数计分地图尺寸(km)，`0`=按回合自动（中国≈6120 / 世界≈14916） | `0` |
| `name_protect` | 昵称隐私保护（默认关，⚠ 改页面显示有被反作弊检测的风险） | 关闭 |
| `log_history` | 捕获点写入 `history.jsonl` | `true` |

**日志**：`logs/main.log` 与 `logs/proxy.log`（1MB 滚动 ×3），写入前强制脱敏（Cookie / Key / 昵称 → `******`）。

---

## ❓ FAQ

<details>
<summary><b>浏览器打开 127.0.0.1:8001 是白屏 / 原始页面没有悬浮窗？</b></summary>

镜像刚启动时 addon 接线有 1~2 秒窗口，**等 1~2 秒刷新一下**即可。若仍无悬浮窗，到「⚙ 设置」确认 `overlay_enabled` 为开。
</details>

<details>
<summary><b>镜像模式需要安装证书吗？</b></summary>

不需要。镜像模式是本地反向代理，**免证书、免接管系统代理**。只有「拦截模式」需要在官网原站上做题时才需要 `--install-cert`。
</details>

<details>
<summary><b>悬浮窗显示「尚未捕获本回合真值」？</b></summary>

坐标需要等进入街景瞬间或答案揭示阶段才捕获。若一直为空，先确认已登录（`.env` 有有效 Cookie），并检查「⚙ 设置」里 `api_poll`（API 直读）为开。
</details>

<details>
<summary><b>Cookie 失效了怎么办？</b></summary>

`fun_ticket` 与 `SESSION` 会过期（SESSION 通常一天）。在镜像站重新登录一次即可自动更新；或到「登录」页手动粘贴新 Cookie。
</details>

<details>
<summary><b>AI 模式报错「街景图片下载失败」</b></summary>

Google 街景图片服务国内无法直连。在 `.env` 设置 `PROXY_URL`（如 `http://127.0.0.1:7890`）后重试；国内图源（腾讯街景）不受影响，始终直连。
</details>

<details>
<summary><b>模型报错 HTTP 400 / 不支持图片</b></summary>

所配模型不支持图片或多图输入。换用 `glm-4.5v`、`qwen-vl-max`、`gemini-2.5-flash` 等视觉模型；仅支持单图的模型运行时加 `--grid`。
</details>

<details>
<summary><b>开启拦截后无法上网？</b></summary>

多为端口被占用。先点「关闭拦截」恢复网络，换端口重启程序；异常退出未恢复代理时，重新运行一次本程序会自动清理。
</details>

<details>
<summary><b>GeoGuessr 页面 403？</b></summary>

GeoGuessr 的 CDN 会封锁部分中国区/数据中心 IP，属上游地缘限制，非本工具问题。请在本机代理环境（Clash 等）下使用 GeoGuessr 链路；图寻不受影响。
</details>

---

## 📦 单文件打包（可选，Windows）

不想装 Python？双击 `build_release.bat`（首次自动创建隔离构建环境），产出两个单文件 exe 到 `dist\`：

| 产物 | 说明 |
|---|---|
| `TuxunHelper-Realtime.exe` | 全套：选择页 + 双平台镜像 + 悬浮窗（一键分数 / 地图标记 / 手机号解析）+ 诱饵免疫 + 登录自动录 Cookie |
| `TuxunHelper-AI.exe` | AI 分析 / 复盘 / 历史 / 会员抽奖（命令行） |

exe 已内置网页与字体资源；配置、Cookie、日志自动生成在 **exe 同目录**。PyInstaller 单文件可能被 Windows Defender 误报，添加信任即可。

---

## 🧩 项目结构

```
tuxun-helper/
├── tuxun_proxy.py    # ★主入口：实时取点 + 双平台镜像 + 悬浮窗注入 + 网页控制 API
├── main.py           # AI 分析 / 复盘 / 抽奖入口
├── game_sources.py   # 对局数据源抽象（图寻 / GeoGuessr / 混合检测）
├── tuxun_agent.py    # 图寻 API 封装 + 街景图下载
├── pano_images.py    # 腾讯街景免 Key 取图（瓦片拼接）
├── ai_client.py      # 统一 AI 后端（Gemini / OpenAI 兼容）
├── geocode.py        # 坐标系转换（WGS84↔GCJ-02↔BD09）+ 逆地理编码
├── applog.py         # 滚动日志 + 自动脱敏
├── tuxun_tui.py      # TUI 后台仪表盘（--tui）
├── web/              # 网页前端（选择页 index.html + 离线地图 + 内置字体 + 海陆掩膜）
├── tests/test_mirror.py   # 镜像单元测试（改 tuxun_proxy.py 后必跑）
├── build_release.bat / package_release.py  # 构建 / 打包
├── requirements.txt
└── .env.example      # 环境变量模板
```

---

## 🙏 致谢

- 原始项目 [haczmrh/tuxun-helper](https://github.com/haczmrh/tuxun-helper)，本项目在其基础上重构扩展
- 感谢社区各类图寻辅助工具（Titanium.Web.Proxy / mitmproxy 方案）提供的实现思路
- [OpenStreetMap](https://www.openstreetmap.org/) / Nominatim、[BigDataCloud](https://www.bigdatacloud.com/)、[高德开放平台](https://lbs.amap.com/)、[Esri](https://www.esri.com/) 提供的地图与地理编码服务
- [语雀图寻文档](https://www.yuque.com/chaofun/tuxun/)、Plonk It、H.M. 等 Meta 社区贡献的知识内容

## 📄 许可证

[MIT](LICENSE) © 2026

---

<div align="center">

**仅供学习和技术交流使用，请尊重游戏规则，理性游戏**

</div>
