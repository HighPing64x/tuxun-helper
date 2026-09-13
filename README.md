<div align="center">

# 🌍 图寻助手 (Tuxun Helper)

**AI 驱动的图寻 / GeoGuessr 双平台游戏辅助工具 —— 支持国内网络环境直连使用**

[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)]()
[![AI](https://img.shields.io/badge/AI-Gemini%20%7C%20GLM--4V%20%7C%20Qwen--VL-8A2BE2)]()

实时取点 · AI 街景分析 · 复盘对答案 · 诱饵坐标免疫 · 国内可用

</div>

---

## 📖 简介

[图寻](https://tuxun.fun) 是一款类似 GeoGuessr 的地理定位游戏。本项目通过**本地代理拦截**与**游戏 API** 双通道获取街景真实坐标，配合视觉大模型与 Meta 知识库，实现"边玩边出点、打完即复盘"的完整辅助链路。

| 模式 | 启动命令 | 原理 | 特点 |
|---|---|---|---|
| 📡 **实时取点** | `python tuxun_proxy.py` | 本地代理拦截街景元数据 + API 直读 | 毫秒级出点；积分赛/每日挑战答案自动揭示 |
| 🤖 **AI 分析** | `python main.py` | 游戏 ID → 多方向街景图 → 视觉大模型定位 | 国内模型直连免代理；适合复盘与学习 |
| 🧾 **复盘模式** | `python main.py --history` | 已结束对局的 API 直带每轮真实坐标 | **免 AI 免代理**，秒出全轮真值与地址 |

**三平台检测模式**（AI 分析按平台调度）：

```
[1] 单图寻        只走图寻 API
[2] 单 GeoGuessr  实验性（已实测：登录验证、每日挑战读取、复盘全链路）
[3] 混合检测      粘贴链接自动识别平台；纯 ID 按 图寻 → GeoGuessr 顺序尝试
```

> [!WARNING]
> 本项目仅供学习和技术交流使用。请在遵守平台服务条款的前提下合理使用，因使用本工具产生的一切后果由使用者自行承担。

## ✨ 功能特性

### 📡 实时取点模式

- **选择页（深色分屏入口）**：启动自动打开 `http://127.0.0.1:18080/`——全屏深色页面中央白点以双向射线分割左右两侧，悬停侧边有放大动效（左=图寻 / 右=GeoGuessr，各带平台图标与文字）；**悬停白点展开圆环菜单**：上=贡献者名单、左下=退出（自动还原代理）、右下=打开 GitHub 仓库
- **双平台镜像模式（`--mirror`）**：本地反向代理把 **图寻** 镜像到 `http://127.0.0.1:8001`、**GeoGuessr** 镜像到 `http://127.0.0.1:8002`——**免装证书、免接管系统代理**，浏览器直接访问本地端口做题；游戏页面内注入悬浮窗（原点/目前/答案 + ⚙ 设置抽屉），CDN 资产与 API 全部经镜像改写，Cookie 会话自动托管
- **一键特定分数（热键落点）**：悬浮窗 ⚙ 设置里启用并调好「目标分数 + 热键」（默认 F9 / 3500 分），游戏中按热键即按计分模型 `分数 = 5000·e^(-10×距离/地图尺寸)` 反推距离、随机方位落点，并经**游戏自己的 ws 通道**（`pin` + `confirm`）提交——与手点地图完全同构，默认关
- **TUI 后台仪表盘（`--tui`）**：终端实时显示端口状态 / 捕获状态 / 日志尾部，`Ctrl+C` 退出（未装 `rich` 自动降级为纯文本）
- **多开互斥**：启动时检测端口占用，弹窗式询问「直接退出 / 结束占用进程」，避免多开导致的端口冲突与配置互相覆盖
- **双平台自动识别**：图寻和 GeoGuessr 的街景都经 Google 服务加载，同一套拦截天然通吃两个平台，按请求来源自动标注「图寻 / GeoGuessr」
- **API 直读（绕过诱饵的被动方案）**：实测图寻的反作弊诱饵只注入在街景元数据里，而浏览器自己请求的 `solo/get` 响应带干净真值——工具被动读取该响应，**零额外请求**；未知全景时才补拉一次（限速 10 秒）
- **积分赛答案揭示**：积分赛对局状态走 websocket，揭示阶段服务器直接下发答案坐标——倒计时结束的瞬间真值自动上屏，天然免疫坐标诱饵
- **原点 / 目前 双坐标**：移动模式回合里，金色标记 = 回合原点（答案，永不移动），绿色标记 = 你当前所在位置（含实时距离）
- **防诱饵（距离 + 移动模式双判定）**：距离变化 ≤150 米 = 同一地点确认正确；无移动回合的远距坐标 = 诱饵排除；可移动回合的远距坐标 = 玩家合法移动（静默）；真值未知时判候选二选一，API 到位自动校准
- **Clash 开关自适应**：开启拦截时自动检测已有系统代理并级联转发（浏览器 → 本工具 → Clash → 互联网），被墙流量照常出得去；关闭/退出时**原样还原**你的代理设置
- **三重防护不断网**：看门狗（代理守卫改写自动夺回、代理线程退出立即还原）+ 启动自愈（清理崩溃残留）+ 全退出路径信号处理
- **双模式登录（免手动抓 Cookie）**：
  - **网页登录（推荐）**：全浏览器流程——本地镜像页内直接登录（图寻微信扫码为页面内轮询机制，无 OAuth 白名单问题），登录 Set-Cookie 经镜像捕获**自动写入 .env**（图寻认 `fun_ticket` / Geo 认 `session`），登录回调 `Location` 与 URL 编码参数一并改写，全程不离开浏览器、不弹任何窗口
  - **弹窗登录（单窗口+官网）**：`--login tuxun|geoguessr` 或 GUI「登录图寻/登录Geo」/ 选择页「弹窗登录」——独立窗口打开官网，后台轮询 Cookie；**官网白屏时窗口内自动提供「改用镜像加载」按钮**，两路捕获殊途同归
- **双镜像常开**：图寻(8001) 与 GeoGuessr(8002) 镜像**未登录也启动**——登录就发生在镜像页里；选择页状态栏实时显示两平台登录态
- **Cookie 自动录入（被动）**：镜像/拦截检测到平台 Cookie 弹窗询问「是否自动录入？」，同意后写入 `.env` 即时生效，拒绝则不再询问
- **双图源拦截**：Google 街景（`GetMetadata`）+ 图寻国内图源（`get(QQ)PanoInfo`）
- **稳健解析**：已知响应结构优先 + 递归扫描兜底，平台改版也不怕
- **内置地图**：Leaflet 暗色地图实时落点，三种瓦片源可选（OSM / 高德 / ArcGIS 卫星），无需申请任何地图 Key
- **坐标系处理**：WGS84 与 GCJ-02（火星坐标）/ BD09（百度坐标）自动互转，国内地图落点精准
- **地址反查**：国内坐标走高德逆地理编码（内置默认 Key），全球坐标走 Nominatim → BigDataCloud 自动回退
- **一键操作**：复制原点/目前坐标、跳转 OSM / Google Maps / 高德，历史落点列表点击回看
- **纯控制台模式**：`--console` 无 GUI 可用，`--proxy` 启动即接管系统代理

### 🤖 AI 分析模式

- **复盘模式（免 AI）**：已结束对局 API 直带每轮真实坐标，自动输出地址与地图链接；`--history` 交互选择或批量复盘；`--ai-review` 让 AI 同题作答并计算公里级误差
- **每日会员抽奖**：`--draw` 一键完成每日挑战页的「抽图寻会员」
- **全自动流程**：输入游戏 ID，自动完成「获取回合 → 下载街景图 → AI 分析 → 反查地址」
- **国内图源取图**：腾讯街景通过官方瓦片免费拼接全景（无需任何 Key），国内对局也能跑 AI
- **多方向视角**：默认 前/右/后/左 4 方向 + 天空视角（判断太阳方位），可选 8 方向
- **TuxunSkill 知识库**：`--skill` 载入内置图寻 Meta 知识库规范（H.M. / 语雀 / PlonkIt 精华）增强分析
- **模型自由**：🇨🇳 国内直连（智谱 GLM-4V、通义 Qwen-VL、SiliconFlow 等任何 OpenAI 兼容接口）/ 🌐 Google Gemini（需配置代理）
- **单图模型适配**：`--grid` 多图拼一张大图，兼容仅支持单图输入的免费模型（如 GLM-4V-Flash）
- **结构化输出**：关键线索 / 大洲 / 国家 / 省 / 州 / 城市 / 置信度 / 十进制坐标
- **直连 Pano**：`--pano` 直接分析任意 Google 街景 Pano ID，无需账号
- **历史与日志**：对局记录自动追加 `history.jsonl`，运行事件写入滚动日志（自动脱敏）

## 🔬 工作原理与实测结论

本项目对图寻的反作弊机制做了系统实测（积分赛 / 每日挑战中国场），核心结论：

1. **诱饵只存在于街景元数据层**：对局进行中，客户端加载街景时收到的元数据坐标可能被替换为假坐标（实测积分赛出现过与答案相差近万公里的假全景）；而浏览器自己请求的 `solo/get` 对局数据是**干净的真实答案**（含进行中对局）。
2. **积分赛（`/point`）是单轮快速赛**：走 `solo/joinRandom` 随机匹配，对局状态走 websocket——等待阶段只有 panoId 不含坐标，**揭示阶段服务器直接下发答案坐标**。
3. **GeoGuessr 同构**：每日挑战等对局的 websocket 揭示阶段同样直接下发答案坐标。
4. **图源与坐标系**：图寻有三种图源——`google_pano`（WGS84）/ `qq_pano`（GCJ-02）/ `baidu_pano`（BD09），工具按来源自动转换，保证国内地图落点不偏移。
5. 因此本工具的策略是：**API 直读为首选真值来源，街景元数据拦截为备用通道，AI 图片分析为原理无关的第三通道**——三者互相校验（`[对照]` 日志持续监控 API 层是否被投毒）。

## 🚀 快速开始

### 1. 安装

```bash
git clone https://github.com/HighPing64x/tuxun-helper.git
cd tuxun-helper

# 国内建议使用镜像加速
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

> 实时取点模式需要 `mitmproxy` 与 `pywebview`（已在 requirements.txt 中），AI 模式二者皆不需要。

### 2. 配置 AI 后端（AI 模式必需，二选一）

在项目根目录创建 `.env` 文件：

**方案 A：国内直连模型（推荐，免费可用）**

以[智谱 AI 开放平台](https://open.bigmodel.cn/)为例：

```ini
OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4
OPENAI_API_KEY=你的智谱APIKey
OPENAI_MODEL=glm-4.5v
```

> `glm-4.5v` / `qwen-vl-max` 等支持一次多图；仅支持单图的模型（如 `glm-4v-flash`）运行时加 `--grid`。

**方案 B：Google Gemini**

到 [Google AI Studio](https://aistudio.google.com/app/apikey) 免费获取 Key：

```ini
API_KEY=AIzaSyXXXXXXXXXXXXXXXXXXXXXXXX
GEMINI_MODEL=gemini-2.5-flash
# 国内网络需要代理（Clash 默认 7890，v2rayN 默认 10809）
PROXY_URL=http://127.0.0.1:7890
```

### 3. 配置平台 Cookie（访问平台 API 时需要）

**方式一（最省事）：自动录入**。开启实时取点的拦截后，用浏览器正常打开 tuxun.fun / geoguessr.com，工具检测到平台 Cookie 会弹窗询问：

> 检测到 [图寻]，是否自动录入cookie？ **[是] / [否]**

点「是」自动写入 `.env` 并即时生效（无需重启）；点「否」不再询问（`config.json` 的 `cookie_declined` 可重置）。

**方式二：手动配置**（`.env` 中添加，`--pano` 或纯复盘模式可跳过）：

```ini
# 指向浏览器插件（Cookie-Editor 等）导出的 JSON 文件（推荐）
TUXUN_COOKIE=cookies.json
# 或直接粘贴 fun_ticket 值 / 完整 Cookie 头
TUXUN_COOKIE=fun_ticket=eyJf...
```

> 插件导出的 JSON 数组可直接使用；`cookies*.json` 已被 `.gitignore` 忽略。
> 手动抓取步骤：浏览器 `F12` → 网络 → 任意 `tuxun.fun` 请求 → 请求标头 → Cookie → 复制 `fun_ticket=` 到分号前的一段。
> GeoGuessr 同理（`GEOGUESSR_COOKIE`，建议插件导出完整 Cookie）。完整示例见 [.env.example](.env.example)。

### 4. 运行

```bash
# ===== 登录（免手动抓 Cookie，两种模式）=====
# 网页登录（推荐）：--mirror 后直接访问镜像页，页面内登录/扫码，Cookie 自动录入
# 弹窗登录（单窗口+官网）：
python tuxun_proxy.py --login tuxun        # 打开图寻官网登录（微信扫码），Cookie 自动写入 .env
python tuxun_proxy.py --login geoguessr    # 同理，GeoGuessr（官网白屏时窗口内提供镜像加载回退）
# GUI 模式也可直接点「登录图寻」/「登录Geo」按钮；选择页点击未登录平台会给出两种模式的选择

# ===== 实时取点 =====
python tuxun_proxy.py --install-cert   # 首次一次：安装证书（装过跳过）
python tuxun_proxy.py                  # 图形界面 → 点「开启拦截」→ 浏览器正常做题
python tuxun_proxy.py --proxy --console  # 纯控制台 + 启动即接管系统代理

# ===== 镜像模式（免证书、免系统代理，双平台）=====
python tuxun_proxy.py --mirror
# 自动打开选择页 http://127.0.0.1:18080/（左图寻 / 右 GeoGuessr / 白点圆环菜单）
# 图寻做题: http://127.0.0.1:8001   GeoGuessr 做题: http://127.0.0.1:8002
# 页面内悬浮窗显示原点/目前/答案，⚙ 设置抽屉调全部功能（含一键特定分数）

# ===== TUI 后台仪表盘 =====
python tuxun_proxy.py --tui --mirror   # 端口/捕获状态/日志尾部实时面板，Ctrl+C 退出

# ===== AI 分析 =====
python main.py                         # 交互菜单（三平台三模式）
python main.py Ge9g36p53Vf2yAL9        # 直接分析指定对局（自动识别平台）
python main.py --skill Ge9g36p53Vf2yAL9        # 带 TuxunSkill 知识库分析
python main.py --directions 8 --model glm-4.5v # 8 方向 + 指定模型
python main.py --pano AF1QipXXXX --open        # 直接分析任意街景 Pano ID

# ===== 复盘（免 AI）=====
python main.py --history               # 列出最近 20 局，交互选择复盘
python main.py --history 5             # 批量复盘最近 5 局（图寻）
python main.py --mode geoguessr --history      # GeoGuessr 最近对局
python main.py 6dea8f92-810e-11f1 --ai-review  # AI 同题作答并对比误差

# ===== 每日抽奖（每挑战抽奖 + 每日任务抽奖，后者完成每日挑战后解锁）=====
python main.py --draw
```

## 📡 实时取点模式使用指南

1. **安装证书**（管理员终端，一次性）：

   ```bash
   python tuxun_proxy.py --install-cert
   ```

   > 程序首次运行会自动在 `~\.mitmproxy\` 生成根证书；也可双击该证书手动导入「受信任的根证书颁发机构」。装完**重启浏览器**。

2. **启动程序**：`python tuxun_proxy.py`，点击 **「开启拦截」**

3. **正常游戏**：浏览器打开 tuxun.fun 或 geoguessr.com 做题——进入街景的瞬间坐标自动落图；积分赛/每日挑战的倒计时结束时答案自动揭示；移动模式回合金色「原点」+ 绿色「目前」双标记

4. **结束游戏**：点 **「关闭拦截」** 或直接退出程序（自动恢复你的代理设置）

> [!NOTE]
> - **Clash/VPN 用户零配置**：开启拦截时自动级联已有系统代理（实测 Clash 7897 级联后 Google 照常访问）；拦截期间请勿在 Clash 里开关「系统代理」，需要切换先点「关闭拦截」
> - 拦截期间浏览器流量经过本机代理（数据不出本机），玩完记得关闭或退出
> - 端口被占用时改用 `--port 8888`；Firefox 用户可加 `--no-system-proxy` 手动指定浏览器代理

> [!NOTE]
> **GeoGuessr 平台说明**（实测结论）：
> - 支持 `geoguessr.com/game/...`、`/challenge/...` 链接与纯 ID（每日挑战 token 可直接用）
> - 挑战/经典对局的进行中回合也会返回真实坐标
> - 已知限制：免费账号无法通过 API 创建经典对局（402）；Duels 系列对战暂不支持读取；panoId 为 hex 编码，工具已自动解码

## ⚙️ 配置参考

### `.env`（AI 模式）

| 变量 | 说明 | 默认值 |
|---|---|---|
| `OPENAI_BASE_URL` | OpenAI 兼容接口地址（国内直连首选） | 无 |
| `OPENAI_API_KEY` | 上述接口的 Key | 无 |
| `OPENAI_MODEL` | 模型名，如 `glm-4.5v`、`qwen-vl-max` | 无 |
| `API_KEY` / `GEMINI_API_KEY` | Google Gemini API Key | 无 |
| `GEMINI_MODEL` | Gemini 模型名 | `gemini-2.5-flash` |
| `PROXY_URL` | HTTP 代理，仅用于访问 Google 街景图 / Gemini | 无 |
| `TUXUN_COOKIE` | 图寻凭证：`fun_ticket=...`、完整 Cookie 头或插件导出的 JSON 文件路径 | 无 |
| `GEOGUESSR_COOKIE` | GeoGuessr 完整 Cookie（实验性平台支持） | 无 |
| `AMAP_KEY` | 高德 Web 服务 Key（**已内置默认 Key 开箱即用**，可替换为自己的） | 内置 |

> 同时配置两组 AI 后端时**国内直连接口优先**；可用 `AI_PROVIDER=gemini / openai` 强制指定。

### `config.json`（实时取点模式，程序自动生成）

| 字段 | 说明 | 默认值 |
|---|---|---|
| `proxy_port` | 本地代理端口 | `8080` |
| `proxy_enabled` | 下次启动自动开启拦截 | `false` |
| `display_delay` | 捕获到坐标后的显示延迟（秒） | `0.4` |
| `map_tiles` | 瓦片源：`osm`（**默认**，经本地 `/tiles/osm/` 代理转发：合规 UA + 磁盘缓存 `cache/`，规避 OSM 对 WebView 类 UA 的 403 封锁）/ `amap` / `arcgis` | `osm` |
| `map_zoom` | 地图初始缩放 | `5` |
| `amap_key` | 高德 Web 服务 Key（已内置默认 Key，开箱即用） | 内置 |
| `upstream_proxy` | 上级代理。**留空 = 自动跟随系统已有代理（Clash 自适应）** | 空（自动） |
| `api_poll` | API 直读：被动读取浏览器自身的 `solo/get` 响应获取真实坐标（绕过诱饵）。**默认关，GUI「API直读」开关** | `false` |
| `cookie_declined` | Cookie 自动录入询问中点过「否」的平台记忆 | 空 |
| `ai_auto` | AI 自动分析：新回合自动抓图分析并对答案（需 .env 配 AI Key） | `false` |
| `mirror_enabled` | 启动时自动开启镜像（也可用 `--mirror` 临时开启） | `false` |
| `mirror_port` | 图寻镜像端口（浏览器访问 `http://127.0.0.1:该端口` 做题） | `8001` |
| `mirror_port_geo` | GeoGuessr 镜像端口 | `8002` |
| `control_port` | 控制端口：选择页主页 + 悬浮窗状态/设置 API（仅本机监听） | `18080` |
| `open_index` | 启动后自动打开选择页 | `true` |
| `oneclock_enabled` | 一键特定分数总开关（悬浮窗 ⚙ 设置里可调） | `false` |
| `oneclock_score` | 一键目标分数（200-4990） | `3500` |
| `oneclock_key` | 一键热键（游戏页面获得焦点时按下生效） | `F9` |
| `map_size` | 一键分数的计分地图尺寸(km)；`0`=按回合自动（中国≈6120 / 世界≈14916） | `0` |
| `anti_decoy` | 反作弊诱饵识别：**距离 + 回合移动模式双判定** | `true` |
| `decoy_window` | 无全景信息时的时间窗判定阈值（秒） | `3.0` |
| `near_m` | 距锚点多少米内视为同一地点（确认正确而非诱饵） | `150` |
| `name_protect` | 隐私保护（**默认关，⚠ 修改页面显示有被反作弊检测的风险**）：DOM 级替换页面上显示的昵称/ID，接口数据永不改动；rules 留空自动学习 | 关闭 |
| `log_history` | 捕获点写入 `history.jsonl` | `true` |

**日志**：运行事件（启动 / 配置读写 / 开关变更 / Cookie 录入事件 / 捕获点 / AI 分析）自动写入 `logs/main.log` 与 `logs/proxy.log`（各 1MB 滚动，保留 3 份）。所有日志行写入前**强制脱敏**——Cookie、API Key、昵称等敏感值自动替换为 `******`；`logs/` 已被 `.gitignore` 排除。

## ❓ FAQ

<details>
<summary><b>AI 模式报错「街景图片下载失败」</b></summary>

Google 街景图片服务在国内无法直连。请在 `.env` 中设置 `PROXY_URL`（如 Clash 的 `http://127.0.0.1:7890`）后重试。图寻 API 与地图链接不受影响，始终直连。
</details>

<details>
<summary><b>实时取点模式浏览器提示「证书不受信任」</b></summary>

mitmproxy 根证书未安装或浏览器未重启。执行 `python tuxun_proxy.py --install-cert` 后**完全退出并重启浏览器**。仍失败则按上文步骤手动双击导入证书。
</details>

<details>
<summary><b>开启拦截后无法上网</b></summary>

多为端口被占用。先点「关闭拦截」恢复网络，换端口重启程序。程序异常退出未恢复代理时，可到 Windows「设置 → 网络和 Internet → 代理」手动关闭，或重新运行一次本程序（启动自愈会自动清理）。
</details>

<details>
<summary><b>国内图源（腾讯/百度街景）轮次的说明</b></summary>

腾讯街景（GCJ-02）与百度街景（BD09）的坐标已自动识别来源并转换：选「高德地图」瓦片源时落点即精确位置。AI 取图方面腾讯图源已支持（官方瓦片免费拼接），百度图源暂无公开免凭证通道——该类回合可用复盘模式或实时取点。
</details>

<details>
<summary><b>Nominatim 地址反查偶尔失败或很慢</b></summary>

Nominatim 是免费公共服务且限速 1 次/秒（已内置限速与缓存）。工具已自动回退到 BigDataCloud 备用源；国内坐标配置高德 `amap_key`（已内置默认 Key）可完全避开 Nominatim。
</details>

<details>
<summary><b>防诱饵会不会误判？</b></summary>

判定基于距离与回合移动模式：150 米内的坐标视为同一地点；无移动回合的远距坐标判诱饵；可移动回合的远距坐标静默跳过（玩家在开车，答案保持回合起点）；真值未知时判候选二选一（红点展示，不排除）。极端情况可随时关闭「防诱饵」开关。
</details>

<details>
<summary><b>模型报错 HTTP 400 / 不支持图片</b></summary>

所配模型不支持图片或多图输入。换用 `glm-4.5v`、`qwen-vl-max`、`gemini-2.5-flash` 等视觉模型；仅支持单图的模型加 `--grid`。
</details>

<details>
<summary><b>Cookie 验证失败 / API 直读失效</b></summary>

`fun_ticket` 与 `SESSION` 有有效期（SESSION 通常一天）。重新导出 Cookie 覆盖 `cookies.json` 即可；开启拦截后用浏览器访问一次平台，自动录入也会提示你更新。
</details>

## 📦 单文件打包（可选，Windows）

不想让使用者装 Python？双击 `build_release.bat`（首次自动创建隔离构建环境）即可产出两个单文件 exe 到 `dist\`：

| 产物 | 说明 |
|---|---|
| `TuxunHelper-Realtime.exe`（约 38 MB） | 实时取点全套：选择页（深色分屏入口）+ 双平台镜像 + TUI 后台 + 悬浮窗（设置抽屉 / 一键特定分数）+ 诱饵免疫 + Cookie 自动录入 + 答案揭示 |
| `TuxunHelper-AI.exe`（约 21 MB） | AI 分析 / 复盘 / 历史 / 会员抽奖（命令行） |

说明：
- **exe 已内置网页版使用教程**：控制台/选择页访问 `http://127.0.0.1:18080/tutorial.md`，或运行 `TuxunHelper-Realtime.exe --tui` 查看后台仪表盘；教程面向 exe 使用者，与 GitHub README 相互独立；
- exe 的配置、Cookie、日志自动生成在 **exe 同目录**；
- 使用 `--skill` 时，把 `TuxunSkill/` 文件夹放到 exe 旁边即可；
- exe 版内置国内直连模型支持；**Gemini 需使用 Python 源码版运行**（体积考量未内置 Gemini SDK）；
- PyInstaller 单文件可能被 Windows Defender 误报，添加信任或从 GitHub Release 下载即可。

## 🧩 项目结构

```
tuxun-helper/
├── main.py           # AI 分析模式入口（三模式调度 / 复盘 / Skill 集成 / 会员抽奖）
├── game_sources.py   # 对局数据源抽象层（单tuxun / 单GeoGuessr / 混合检测）
├── tuxun_agent.py    # 图寻 API 与 Google 街景封装
├── pano_images.py    # 腾讯街景免 Key 取图（瓦片拼接 + 方向视图裁剪）
├── ai_client.py      # 统一 AI 后端（Gemini / OpenAI 兼容接口）
├── geocode.py        # 坐标系转换（WGS84↔GCJ-02↔BD09）+ 逆地理编码
├── applog.py         # 统一日志（滚动文件 + 自动脱敏）
├── tuxun_proxy.py    # 实时取点模式入口（本地代理 / 诱饵判定 / Cookie 自动录入 / 双镜像 / 一键分数）
├── tuxun_tui.py      # TUI 后台仪表盘（--tui，端口/捕获状态/日志尾部）
├── gui.html          # 实时取点模式界面（Leaflet 地图 / 原点目前双坐标）
├── web/              # 选择页 index.html + exe 内置教程 tutorial.md（打包进 Realtime exe）
├── build_release.bat # 单文件 exe 构建脚本（见「单文件打包」）
├── docs/
│   └── 原项目README.md   # 原始项目说明存档
├── TuxunSkill/       # （可选，本地）图寻 Meta 知识库，--skill 时载入；已被 .gitignore 排除
├── requirements.txt
├── .env.example      # .env 配置模板
└── .gitignore        # 已忽略 .env / config.json / cookies*.json / TuxunSkill / logs / 构建产物
```

> 运行产物（`config.json`、`history.jsonl`、`logs/`）与打包产物（`dist/`、`release/`、`.build-venv/`）同样已被 `.gitignore` 排除。

## 🙏 致谢

- 原始项目 [haczmrh/tuxun-helper](https://github.com/haczmrh/tuxun-helper)，本项目在其基础上重构扩展
- 感谢社区各类图寻辅助工具（Titanium.Web.Proxy / mitmproxy 方案）提供的实现思路
- [OpenStreetMap](https://www.openstreetmap.org/) / Nominatim、[BigDataCloud](https://www.bigdatacloud.com/)、[高德开放平台](https://lbs.amap.com/)、[Esri](https://www.esri.com/) 提供的地图与地理编码服务
- [语雀图寻文档](https://www.yuque.com/chaofun/tuxun/)、Plonk It、H.M. 等 Meta 社区贡献的知识内容（TuxunSkill）

## 📄 许可证

[MIT](LICENSE) © 2026

---

<div align="center">

**仅供学习和技术交流使用，请尊重游戏规则，理性游戏**

</div>
