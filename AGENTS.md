# AGENTS.md — 图寻助手 (Tuxun Helper) 交接文档

> 本文件为**项目总契约**：给没有上下文的新 AI（或新会话）完整交代代码库、约定、状态与下一步。
> 所有命令在项目根目录（`G:\.Selfmade\tuxun-helper`）运行；工作平台为 Windows + Git Bash + Python 3.11。
> **标注「待确认」处 = 交接时尚未查实，务必先验证再行动，不要臆断。**

---

## 1. 项目目标

为 **[图寻 tuxun.fun](https://tuxun.fun)**（国内版 GeoGuessr 类游戏）与 **[GeoGuessr](https://www.geoguessr.com)** 打造一个本地辅助工具。核心思路：

1. **本地反向代理整站镜像**：把 tuxun.fun / geoguessr.com 完整网页代理并路由到 `127.0.0.1`（图寻 → `:8001`，GeoGuessr → `:8002`），免证书、免接管系统代理即可做题。
2. **网页前端为唯一界面**（2.0 起）：选择页（`http://127.0.0.1:18080/`）深色分屏选平台 → 登录（官网镜像内自动抓 Cookie / 手动输入 Cookie，自动保存到 `.env`）→ 进入本地镜像的、与官网「基本一模一样」的游戏页面，并注入悬浮窗功能（原点/目前/答案 + ⚙ 设置抽屉 + 一键特定分数）。
3. **为 AI 分析 / 复盘提供第二通道**（`main.py` CLI）：游戏 ID → 街景图 → 视觉大模型定位；已结束对局走 API 直读真值，免 AI。
4. **兼容用户自制扩展**（当前形态：前端 `web/index.html` + `MirrorRewrite` 改写脚本是扩展点；注入物 `_OVERLAY_SCRIPT` 可自定义）。
5. **反作弊安全**：优先**只读 + 被动**（API 直读 / 拦截读响应），绝不主动提交异常行为；挖掘接口是为了更准，不是更激进。

> 平台授权声明已由用户以「设置目标」形式确认：他本人持有并提供了图寻 Cookie 用于测试（在 `cookies.json`，会过期）。**不得把该 Cookie 的任何值写入代码、文档或提交。**

---

## 2. 技术栈、版本、包管理器

| 项 | 值 |
|---|---|
| 语言 | **Python 3.9+**（实测 3.11.9，Windows 11） |
| 包管理器 | pip + `requirements.txt`（无 lockfile；无 poetry/uv） |
| 关键第三方 | `requests`、`python-dotenv`、`mitmproxy>=10`（实测 **11.0.2**）、`rich>=13`（TUI 可选）、`pillow`（--grid 可选）、`google-genai`（Gemini 可选） |
| 无数据库 | 持久化 = JSON 文件 + JSONL 追加（见 §7） |
| 打包 | **PyInstaller onefile**（构建环境 `.build-venv`，隔离） |
| 前端 | 原生 HTML/CSS/JS（无框架）+ 本地 `web/vendor/leaflet.js`（离线地图） |
| 备选语言 | 用户提到可用 C# 编译环境，但**当前代码全为 Python**，未到迁移决策点 |

已安装验证过：`mitmproxy 11.0.2`、`Flask 3.1.0`（未用）、`requests 2.32.5`、`rich 14.3.2`。

---

## 3. 安装 / 运行 / 测试 / 构建命令

### 安装
```bash
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 运行
```bash
# 实时取点 + 镜像 + 网页前端（主入口）
python tuxun_proxy.py --mirror --console
#   → 自动打开选择页 http://127.0.0.1:18080/
#   → 图寻镜像 http://127.0.0.1:8001   GeoGuessr 镜像 http://127.0.0.1:8002

python tuxun_proxy.py --install-cert   # 首次一次性：装 mitmproxy 根证书（代理拦截需要）
python tuxun_proxy.py --tui --mirror   # Rich 后台仪表盘
python tuxun_proxy.py --proxy          # 启动即接管系统代理（拦截模式）

# AI 分析 / 复盘 / 抽奖（第二入口 CLI）
python main.py                          # 交互菜单
python main.py <game_id>                # 直接分析指定对局（自动识别平台）
python main.py --history                # 复盘最近 20 局（免 AI）
python main.py --draw                   # 每日会员抽奖
```

### 测试（改 `tuxun_proxy.py` 后必跑）
```bash
python tests/test_mirror.py        # 当前 15 项断言全通过（2026-09-13 实测）
```

### 构建 / 打包
```bash
build_release.bat       # 产出 dist/TuxunHelper-Realtime.exe（全部前端内嵌）+ dist/TuxunHelper-AI.exe
python package_release.py   # 打包 release/TuxunHelper-win64.zip（本地 zip，供用户测试，不推送）
```

### lint
无正式 lint 配置（无 flake8/ruff 配置)；代码里出现 `# noqa` 约定。语法自检：
```bash
python -m py_compile tuxun_proxy.py main.py game_sources.py tuxun_agent.py \
  pano_images.py ai_client.py geocode.py applog.py tuxun_tui.py
```

---

## 4. 目录结构与关键文件

```
tuxun-helper/
├── tuxun_proxy.py    # ★主入口：实时取点 + 双平台镜像 + 控制 API + 网页登录链路（2526 行）
├── main.py           # AI 分析模式入口（三模式调度 / 复盘 / Skill / 抽奖）
├── game_sources.py   # 对局数据源抽象层（TuxunSource / GeoGuessrSource / 混合检测）
├── tuxun_agent.py    # 图寻 API 封装 + 街景图 URL/下载（TuxunAgent）
├── pano_images.py    # 腾讯街景免 Key 取图（瓦片拼接）
├── ai_client.py      # 统一 AI 后端（Gemini / OpenAI 兼容接口）
├── geocode.py        # 坐标系转换 WGS84↔GCJ-02↔BD09 + 逆地理编码
├── applog.py         # 滚动日志 + 自动脱敏
├── tuxun_tui.py      # TUI 后台仪表盘
├── web/
│   ├── index.html    # ★选择页/仪表盘/地图/设置/登录/教程（全单文件，678 行）
│   ├── tutorial.md   # exe 内置教程（挂 http://127.0.0.1:18080/tutorial.md）
│   └── vendor/leaflet.{js,css}  # 离线地图
├── tests/test_mirror.py  # 镜像单元测试（直接运行，15 断言）
├── TuxunSkill/        # （可选本地）图寻 Meta 知识库，--skill；已 gitignore
├── build_release.bat / package_release.py  # 构建/打包
├── docs/原项目README.md  # 原始项目存档
└── .env.example       # 环境变量模板
```

### `tuxun_proxy.py` 关键类/函数（均带行号，可点跳）
- `TuxunApp`（:1655）— 应用主体：捕获分发、Cookie 落袋、代理托管、一键分数状态
  - `verify_login` 相关不在本文件；Cookie 验证在 `TuxunAgent.verify_login()`（tuxun_agent.py:118）
- `MirrorRewrite`（:812）— 镜像改写的核心 addon：URL 改写、CDN 改道、Set-Cookie 剥离、**悬浮窗注入**
- `MirrorCookieJar`（:998）— 镜像会话 Cookie：`.env` 优先，会话文件回退
- `ControlApiHandler`（:1213）— 控制/网页 API（state/settings/points/login/*/manual-cookie/cookie/tiles/…）
- `TuxunInterceptor`（:342）— 代理拦截坐标捕获 + 诱饵判定
- `SoloApiReader`（:1559）— API 直读（被动读浏览器自身响应取真值）
- `ProxyServer`（:1458）/ `start_mirror_server`（:2346）/ `main()`（:2389）
- `_OVERLAY_SCRIPT`（:480）— 注入悬浮窗的脚本模板；`_META_CSP_RE` / `_META_CSP`（:672）
- `MIRROR_SPECS`（:2336）— 平台规格表（origin / cdn_origin / env_key / session_file）

### 端口约定（config.json 可改，以下为默认）
`18080` 控制API+选择页 ｜ `8001` 图寻镜像 ｜ `8002` GeoGuessr 镜像 ｜ `8080` 本地代理端口（拦截模式）

---

## 5. 代码风格与开发约定

- **中文注释与提交信息**（本项目惯例）；docstring 说明“为什么”。
- 命名：类 `PascalCase`，函数/变量 `snake_case`；模块级常量 `_UPPER`。
- 平台分支用 `WINDOWS = sys.platform == "win32"` 判断；Windows 特有用 winreg。
- **日志一律经 `applog`**：滚动 1MB × 3 + **写入前强制脱敏**（Cookie/Key/昵称 → `******`）。
- 联网会话原则：
  - 图寻 API：`trust_env=False` 直连（国内可达），**不走系统代理**；
  - Google 街景图/Gemini：可走 `PROXY_URL`（国内需代理）。
- 前端 `web/index.html` 为单文件 `SPA`（section 切换 `go()` / `v-dash/v-map/v-settings/v-login`）；改它要同步控制 API 端点。
- **改 `tuxun_proxy.py` 后必须** `python tests/test_mirror.py`。
- 提交/发布纪律：见 §13 禁止事项（不自动 push / release）。

---

## 6. 环境变量（`: .env`，**只列名，禁写实际值/密钥**）

| 变量 | 用途 |
|---|---|
| `TUXUN_COOKIE` | 图寻凭证：`fun_ticket=...`、完整 Cookie 头、或 Cookie-Editor 导出的 **JSON 文件路径**（推荐，如 `cookies.json`） |
| `GEOGUESSR_COOKIE` | GeoGuessr 完整 Cookie（实验性） |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL` | OpenAI 兼容视觉模型（国内首选，如 glm-4.5v） |
| `API_KEY` / `GEMINI_MODEL` | Google Gemini（需 `PROXY_URL`） |
| `AI_PROVIDER` | 强制后端：`openai` / `gemini`（默认双配时国内直连优先） |
| `PROXY_URL` | HTTP 代理，仅用于 Google 街景 / Gemini；Clash 常 `http://127.0.0.1:7890` |
| `AMAP_KEY` / `AMAP_JS_KEY` | 覆盖内置高德 Key（国内逆编码） |
| `AI_TIMEOUT` | 可选，AI 超时秒数 |

✓ `.env`、`config.json`、`cookies*.json`、`logs/`、`cache/` 均已 `.gitignore`，不入库。

---

## 7. 数据 / 持久化（无数据库）

| 文件 | 内容 | 规格 |
|---|---|---|
| `.env` | 全部配置/凭证 | 程序 `upsert_env_line` 写入；被 gitignore |
| `config.json` | 运行配置（端口、开关，见 README §配置参考） | 自动读改写 |
| `history.jsonl` | 捕获点/对局记录 | 追加 JSONL，`log_history` 控制 |
| `logs/main.log`、`logs/proxy.log` | 运行日志 | 1MB×3 滚动 + 脱敏 |
| `cache/tiles/` | OSM 瓦片磁盘缓存 | 自动 |
| `_mirror_session_{tuxun,geo}.txt` | 镜像登录会话兜底（jar 刷新） | 自动；`MirrorCookieJar` 管理 |
| `cookies.json`（本地） | **用户真实图寻 Cookie（会过期）** | ⚠ 含私密值：禁止提交/扩散/写入文档 |

> 无数据库、无迁移。若新增持久化偏好用 JSON / JSONL 平铺，保持文件表意清晰。

---

## 8. 当前 git 状态

- 分支：`main`（默认分支，PR 用）
- 线上/本地：本地 **ahead `origin/main` 5 个提交**
- 最近提交：`c171df7` — “v2.0.0 全网页版重构：本地仅剩 后台 CLI + 网页前端”
- 未提交变更（工作区）：
  - `M README.md`、`M package_release.py`、`M tests/test_mirror.py`、`M tuxun_proxy.py`、`M web/index.html`、`M web/tutorial.md`
- 未跟踪：
  - `?? repro_tmp.py`、`?? verify_login.py` — 临时验证脚本（`_` 前缀未用，会显示 untracked；**完成后建议删除或改 `_` 前缀**）
- git 用户：HighPing64x

---

## 9. 任务状态

### 已完成的过去任务（里程碑，按 git log 倒序）
1. **v2.0.0 全网页版重构**（`c171df7`）— 移除 pywebview/原生窗口，本地仅剩后台 CLI + 网页前端。
2. **双模式登录**（`00482ce`）— 全网页端（镜像内登录自动落袋）+ 弹窗登录（白屏回退）。
3. **内置登录窗口 + OSM 本地瓦片代理 + 选择页暗化修复**（`29750e9`）。
4. **固化发布流程**（`f7d5084`）— 本地打包 zip，推送/发布一律先经用户确认。
5. **瓦片源默认高德 + 横幅端口说明**（`2663ed2`）。
6. 早期：双平台镜像（`4f410e0`）、每日挑战计分通道（`18ffeb1`）、TUI/一键分数/多开互斥（`3dd5821`）。

### 正在做的任务（本次交接时）
- **真实环境验证（2026-09-13 已完成；2026-09-19 修复镜像不注入）**：单元测试 15/15 通过；真实 `tuxun_proxy.py --mirror --console` 下选择页/state/settings/tutorial 全部 200；图寻镜像真实实例 `200 len=13324 __TUXUN_OVERLAY__=True` + CDN `/cdn/` 改写生效。**2026-09-19 新增**：冷启动实测发现真实实例镜像不注入（`len=1226` 原始 HTML、无 overlay），根因定位为 mitmproxy 11 同进程多 DumpMaster 冲突，重构为单 master 多 mode + `MirrorUnifiedAddon` 后修复（`8001 → 200 len=13336 overlay=True cdnRewrite=True`，日志含 `[RWDBG] tuxun request/response fired`），详见 §10 任务 A。
- **真实 Cookie 验证（已完成，时效性）**：用户 `cookies.json` 验证有效，UID 1074205；登录落袋链路 e2e 通过（§10 任务 B/C）。

### 未完成 / 待办
- 浏览器内人工走一遍「选择页 → 镜像内登录 → 做题」真实点击流（受 Cookie 时效与 GeoGuessr 上游 403 限制，未自动化）。
- v2.0.0 未提交的 7 个修改文件（`tuxun_proxy.py` `web/index.html` `web/tutorial.md` `README.md` `AGENTS.md` `package_release.py` `tests/test_mirror.py`）待用户确认后提交。

---

## 10. 下一步原子任务（按优先级，附带验收标准）

> **结论（2026-09-19 实测，根因已修复）**：「真实实例镜像不注入」的根因是 **mitmproxy 11 同一进程多 DumpMaster 互踩全局 proxyserver 注册表**，导致镜像 addon 钩子完全不触发（与早期「残留进程/启动竞态」的推断无关，后者为误判）：
> - 复现三连：单 DumpMaster（reverse tuxun）→ 钩子正常；同进程两个 DumpMaster → 端口互抢 + 事件循环混乱 + addon 完全不触发（镜像端口能返回上游原始 HTML，但无 `[RWDBG]`、无 overlay、无 `/cdn/` 改写）；单 master + 双 reverse mode → 全部正常。
> - 修复（2026-09-19）：改为**单一 DumpMaster** 承载全部监听。`ProxyServer._mode_list()` 组装 `reverse:tuxun@8001 + reverse:geoguessr@8002 + regular/upstream@8080`；新增 `MirrorUnifiedAddon` 按 `flow.client_conn.sockname[1]`（客户端连入的本地端口）分发到 图寻镜像 / Geo镜像 / 拦截 三套处理链；`_apply_mode` 运行时整体重配 mode 列表（8080 槽热切换级联）。
> - 验证：冷启动后 `:8001/` 返回 `200 len=13336 __TUXUN_OVERLAY__=True` + `/cdn/` 改写生效，日志含 `[RWDBG] tuxun request/response fired`；`:8002/` 200；`:8001/cdn/tuxun/favicon.ico` 改道 `b68res.daai.fun` 返回 200。
> - 遗留小陷阱：端口刚 LISTENING 时 addon 可能尚未完成接线，此窗口访问会拿到原始上游 HTML（无 overlay 无 `[RWDBG]`）。启动后等 1-2s 再探即可，无需清端口残留。

### 任务 A【已完成·2026-09-19 修复】：真实实例镜像注入差异
- **状态**：根因 = mitmproxy 11 多 DumpMaster 互踩全局注册表；已改为单 master 多 mode + `MirrorUnifiedAddon` 分发修复。同 memory `real-vs-isolated-mirror-injection-gap`（已修复）。
- 回归验证：`python tuxun_proxy.py --mirror --console` → 等 1-2s → 访问 `:8001/` 应含 `__TUXUN_OVERLAY__`，日志含 `[RWDBG] tuxun response fired`。

### 任务 B【已完成】：验证用户 Cookie 登录态
- **状态（2026-09-13 实测）**：`verify_login()` 返回 **UID 1074205**（via `/api/get_profile`），用户 `cookies.json` 仍有效。
- 若日后失效（True None/API 401/403）：让用户重新导出 Cookie 覆盖 `cookies.json`，再重跑：
  ```python
  from tuxun_agent import TuxunAgent
  print(TuxunAgent(open('cookies.json', encoding='utf-8').read()).verify_login())
  ```

### 任务 C【已完成】：登录 → 镜像做题端到端确认
- **状态（2026-09-13 实测，隔离端口 18099/8101/8102）**：登录落袋链路验证通过：
  `.env TUXUN_COOKIE`（预检空 → `note_mirror_login` 后写入 ✓）→ 环境变量热更新 ✓ → 落袋后 `verify_login()` 再验证 UID 1074205 一致 ✓ → 镜像首页 `200 overlay=True` ✓。
- **会话文件 `_mirror_session_tuxun.txt` 仅在 `MirrorCookieJar.update()`（真实 Set-Cookie 流）写入**；`note_mirror_login` 只写 `.env`。测试若直接调后者发现文件缺失属正常，非 bug。
- 剩余人工项：浏览器过一遍选择页 → 镜像内登录 → 做题的真实点击流（受 Cookie 时效 + GeoGuessr 上游 403 限制，未自动化）。

### 任务 D【已完成】：清理
- `repro_tmp.py`、`verify_login.py` 与全部 `_*.log`/`_patch*` 诊断临时文件已删除（工作树仅剩正式修改文件）。

---

## 11. 已知问题、阻塞、风险

1. **[已修复 2026-09-19] 真实实例镜像不注入**：见 §10 任务 A。根因 = mitmproxy 11 同进程多 DumpMaster 互踩全局 proxyserver 注册表（非残留进程/竞态，旧结论作废）。已重构为单 master 多 mode + `MirrorUnifiedAddon` 端口分发；真实实例 `200 overlay=True` + `/cdn/` 改写已验证。
2. **GeoGuessr 上游 403**：本地数据中心/中国区 IP 直连 `geoguessr.com` 返回 `403 Forbidden`（Comcast/CDN 地缘封锁），**非本地 bug**（已入 memory：`geo-mirror-403-upstream-block`）。开发者需在本机代理环境（Clash 等）下才能完整测试 GeoGuessr 链路；镜像层代码本身应健康。
3. **Cookie 时效**：图寻 `fun_ticket` 与 `SESSION` 会过期（SESSION 通常 1 天）。**最后一次验证：2026-09-13 UID 1074205 有效**。失效症状：`verify_login()` 返回 None、访问 API 401/403。失效后需用户重新导出覆盖 `cookies.json`。
4. **mitmproxy 11 与 `DumpMaster`**：`'AddonManager' object is not iterable`（09-13 12:28 一次性）与本次定位的多 master 冲突同源——mitmproxy 11 的全局 proxyserver/事件循环状态不支持同进程多 master。现已收敛为单 master（§10 任务 A），不应再出现；若复现「镜像不注入」按任务 A 回归，不要改回多 master 架构。
5. **反作弊红线**：不注入主动提交逻辑到平台（只被动读）；`name_protect`、诱饵判定不得激进改写；保持工具“只读优先”。
6. **[2026-09-24 实测·已修正] 图寻 /api 双重 403：(a) 账号级 + (b) 镜像上游 TLS 指纹**：
   - **(a) 账号级（先发现）**：旧 `fun_ticket`（UID 1074205 那份）携带即 403。**清除该标记 Cookie 后，直连的 `/api/v0/time/getTime`、`/api/v0/user/getLoginTicket`（活二维码）、`/api/v0/login/loginByWXPublicCode` 全部 200**（requests/urllib 普通 TLS，Cookie/无 Cookie/走 7897 代理都是 200）。→ 账号级限制已随清凭证解除。
   - **(b) 镜像上游 TLS（持续走神真凶，后定位）**：**凡经镜像（`:8001`）转发的 /api 一律 403，连 `getTime` 也 403，与客户端 Cookie 无关**（无 Cookie 也 403）；而镜像转发页面 HTML（`tuxun.fun/`）及 `/cdn/` 静态能 200。→ **图寻 CDN 对 /api 只放行真实浏览器/普通 HTTP 客户端 TLS，mitmproxy 上游 TLS 被拒 403**。这正是镜像做实时取真值的硬堵点：浏览器向镜像索要 /api 数据时，镜像用自己的(被识别)TLS 去上游取，被拦 → 页面"服务器走神"。
   - **结论/出路**：换号、清 Cookie、Ctrl+F5 都救不了 (b)。可行方向 = **非 mitmproxy 的取真值通道**（真实浏览器/普通 HTTP 客户端直连 /api 已实测 200）；先前"playwright 无需且无效"的结论仅对"被标记账号"成立，对 (b) 不成立——普通 TLS 直连可行即证。已给 `MirrorRewrite.response` 加纯被动诊断：首次见图寻 /api 上游 403 即 `logger.warning` 并置 `app._tuxun_flagged`（暴露于 `/state.tuxun_flagged`）。红线只读被动，不硬闯。

---

## 12. 关键决策记录（架构为什么这样）

- **镜像优先**：整套流程免证书、免接管系统代理；网页 = `127.0.0.1:8001/8002`。选页/控制走独立 `18080`。
- **真值三级依赖**：API 直读（被动，首选）> 街景元数据拦截（备用）> AI 图片分析（原理无关第三通道）。诱饵只在元数据层，API 直读是绕过诱饵的被动方案。**2026-09-19 起 `api_poll` 默认开启**：镜像模式不走系统代理，GetMetadata 拦截不触发，只有解析浏览器自身 solo/get 响应才能出答案；该解析为被动读、零额外请求（仅未知全景时补拉一次，10s 限速），与反作弊红线一致。
- **Cookie 可落袋**：镜像内登录 Set-Cookie 被 `MirrorRewrite._fix_set_cookie` 捕获 → `MirrorCookieJar` → 持久化 `.env`，重启仍可用；手动输入走 `/manual-cookie` 同样落袋。
- **全网页版（2.0）**：本地无原生窗口，前端单文件承载所有页面；PyInstaller 内嵌 `web/`。
- **反作弊防线**（来自实测）：距离变化 ≤150m=同地点；无移动回合远距=诱饵排除；可移动回合远距=合法移动（静默）；候选二选一等 API 校准。
- **发布流程**：`build_release.bat` 出两个 exe → `package_release.py` 打包 zip（本地）→ 用户验收后才可能推送/发布。

---

## 13. 给下一个 AI 的「禁止事项」与「优先事项」

### 禁止事项（硬性）
1. **不要自动 `git push` / `gh release create`**：只做本地提交，把变更点列给用户预览；用户明确说“推送/发布的才推/发”。
2. **不要把 `cookies.json` / `.env` 的任何值写进代码、提交、文档或日志示例**。
3. 改 `tuxun_proxy.py` 不跑 `python tests/test_mirror.py` = 不允许直接提交。
4. 镜像不注入的根因已定为 mitmproxy 11 同进程多 DumpMaster 冲突并修复（§10 任务 A）；若再复现，按任务 A 的回归方式（等 1-2s 再探）排查，**不要改回多 master 架构**。
5. 不把 GeoGuessr 403 当成本地 bug 修（它是地缘封锁，改镜像层会弄坏本机正常用户）。
6. 不引入任何可能触发平台封号的激进行为（主动灌包/高频探测/伪装提交等）。
7. 打包 zip 供用户测试前，若改了 `tuxun_proxy.py` / `web/` 必须先重建 `dist/TuxunHelper-Realtime.exe`（`build_release.bat`）。

### 优先事项（按序）
1. **先跑** `python tests/test_mirror.py` 确认基线 15/15。
2. ~~复现任务 A 的镜像注入差异并定位根因~~（**已完成 2026-09-19**：根因 = 多 DumpMaster 冲突，已单 master 化修复，见 §10 任务 A）。
3. **快速验证任务 B 的 Cookie 登录态**（时效性强）。
4. 走通任务 C 的“选平台 → 登录落袋 → 镜像做题”端到端。
5. 完成后再谈用户提到的重构方向（语言/架构调整需先给出方案让用户审核，不擅动）。

---

*本文档由交接会话生成（2026-09-13）。符号约定：`★` = 高频改动文件；「待确认」= 交接时未查实的推断，行动前必须验证。*