# 图寻助手（tuxun-helper）API 接口汇总

> 汇总本项目所有**入站 HTTP 接口**（本地控制 API）与**对外调用的平台 / 第三方接口**。
> 文档只描述接口形态、参数、返回字段与调用来源，**不包含任何 Cookie / Key / 令牌的实际值**。
> 全部对外调用遵循「只读 + 被动」反作弊红线：只读平台数据，不主动提交、不伪装、不灌包。
>
> 生成方式：对 `tuxun_proxy.py` / `tuxun_agent.py` / `game_sources.py` / `main.py` /
> `geocode.py` / `pano_images.py` / `ai_client.py` 的静态检索与关键路由通读（2026-09-25）。

---

## 目录

1. [通用响应 / 请求约定](#通用请求响应约定)
2. [一、本地控制 API（`127.0.0.1:18080`）](#一本地控制-api12700118080)
3. [二、图寻原生 API（`https://tuxun.fun`）](#二图寻原生-apituxunfun)
4. [三、GeoGuessr 原生 API](#三geoguessr-原生-api)
5. [四、第三方 / 地图服务](#四第三方--地图服务)
6. [五、端口与进程约定](#五端口与进程约定)
7. [六、反作弊红线](#六反作弊红线)

---

## 通用请求 / 响应约定

- **JSON 统一形态**：绝大多数接口 `Content-Type: application/json; charset=utf-8`；成功/失败都在一个对象里用字段区分（见各接口）。
- **脱敏**：任何写日志的请求/响应体都先经 `applog.sanitize_json`（Cookie / Key / 昵称 → `******`）。
- **缓存**：控制 API 的 JSON 一律带 `Cache-Control: no-store`，避免前端拿到过期状态。
- **CORS**：控制 API 的 JSON 与资源带 `Access-Control-Allow-Origin: *`，并处理 `OPTIONS` 预检——这样 **镜像页（`8001`/`8002`）里的悬浮窗能跨端口轮询 `18080`** 的 `/state`、`/points`。
- **鉴权**：控制 API 只绑 `127.0.0.1`，外部不可达，因此无额外 Token。对平台的调用鉴权凭 `.env` 内 Cookie（`TUXUN_COOKIE` / `GEOGUESSR_COOKIE`）。

---

## 一、本地控制 API（`127.0.0.1:18080`）

- 端口 `127.0.0.1:<control_port>`，默认 `18080`（`config.json["control_port"]` 可改，前端会同步替换）。
- 服务：`http.server.ThreadingHTTPServer`，`ControlApiHandler`（[tuxun_proxy.py:1529](file:///f:\.Selfmade\tuxun-helper\tuxun_proxy.py)）。
- 未命中任何路由 → `404 {"error":"not found"}`。

### GET —— 状态/数据查询

#### `/state` —— 全量状态（悬浮窗 + 选择页都靠它）
返回对象（字段含义如下）：

```jsonc
{
  "version": "2.0.0",                          // 程序版本
  "origin": {"lat": 39.9042, "lng": 116.4074}  // 本回合原始落点（金标）
  , "origin_trusted": true,                    // 原点是否来自可信真值（API 直读）
  "current": {"lat":..., "lng":..., "address": "..."}, // 当前（金标下方，可随移动更新）
  "answer": {"lat":..., "lng":..., "address": "..."},  // 最终答案（蓝标）
  "coord": "wgs84",                            // 当前坐标参考系
  "round_move": false,                         // 本回合是否发生过合法移动
  "decoys": 0,                                 // 本回合诱饵计数
  "candidates": [],                            // 候选列表（二选一等）
  "origin_addr": "...", "current_addr": "...", "answer_addr": "...",
  "mirrors": {"tuxun": 8001, "geoguessr": 8002}, // 镜像端口实际绑定
  "intercept": false,                          // 系统代理是否由本工具接管
  "tuxun_flagged": false,                      // ⚠图寻 /api 曾返 403 被标记（只读诊断）
  "pending_cookie": "图寻",                    // 等待落袋的平台（null 则无）
  "cookies": {"tuxun": true, "geoguessr": false}, // 是否已有对应平台 Cookie（只暴露有无）
  "settings": { /* 见 /settings 字段 */ }
}
```
说明：`current/answer` 含 `lat/lng/address`；诱饵与候选都只统计 **当回合**。`tuxun_flagged` 为纯被动诊断位。

#### `/settings` —— 设置快照（只读）
返回 `{"status":"ok","settings": {...}}`。`settings` 由可编辑键 + 派生态构成：
`name_protect`(布尔开关)、`intercept`(系统代理接管态)、`version`。

#### `/points` —— 最近 60 条捕获点
返回 `{"points":[{lat,lng,coord,source,kind,pano,time,address,from_origin_m}]}`，按时间倒序取最后 60 条。
字段：`from_origin_m` = 距原点距离（米）；`source` = 来源（如 `GeoGuessr·API直读`）。
用于历史标记、误差复盘、诱饵排除对照。

#### `/phone-cc?num=<11位中国手机号>`
按前 7 位号段查省/市（内置 `web/phone_cc.json.gz`）：
- 合法 → `{ok:true, num, cc: "3411", province, city}`
- 未收录 → `{ok:false, num, msg:"未收录该号段"}`
- 非法 → `{ok:false, num, msg:"仅支持 11 位中国手机号"}`（均 200）
数据源 xluohome/phonedata，仅供学习。

#### 静态资源 / 前端
| 路径 | 说明 |
|---|---|
| `/` 或 `/index.html` | 选择页 SPA（`web/index.html`），正文 `text/html` |
| `/tutorial.md` | 内置教程（Markdown 文本） |
| `/lite.html` | Lite 外置悬浮小窗（轮询 `/state`、`/points` 渲染） |
| `/vendor/<file>` | `leaflet.js` / `leaflet.css`（`os.path.basename` 防目录穿越） |
| `/tiles/osm/{z}/{x}/{y}.png`（支持 `@2x`） | 本地 OSM 瓦片代理（见 4.3）。`502` = 当前不可用 |

任何资源缺失 → `404`；瓦片上游全坏 → `502`。

### POST —— 命令 / 写入（`application/json`）

| 路径 | 请求体 | 行为与返回 |
|---|---|---|
| `/official-login/{tuxun\|geoguessr}` | — | 启动官网官方登录；返回 `{status,message,url}`，`url` 为对应官网地址（图寻 → `https://tuxun.fun/`，Geo → `https://www.geoguessr.com/`）。未知平台 → 404 |
| `/manual-cookie` | `{platform, cookie}` | 手动粘 Cookie 落袋（`save_manual_cookie`），成功 `200 {status:"success"}`，失败 `400` |
| `/login/{tuxun\|geoguessr}` | — | 返回该平台**镜像登录页**地址 `{status:"success", url}`（2.0 全网页版，登录发生在镜像页内） |
| `/cookie` | `{accept: bool}` | 拦截模式下的被动 Cookie 询问应答入口，返回 `answer_cookie_prompt` 结果 |
| `/proxy/start` | — | 接管系统代理（拦截模式），写回 `config.json` |
| `/proxy/stop` | — | 还原系统代理，写回 `config.json` |
| `/settings` | 键值对 | 仅接受 `EDITABLE_KEYS`；`name_protect` 以布尔开关提交、只翻 `enabled`。返回 `{status:"ok", settings}` |
| `/shutdown` | — | 还原系统代理后后台线程优雅退出，返回 `{status:"exiting"}` |

**`EDITABLE_KEYS`（[tuxun_proxy.py:1546](file:///f:\.Selfmade\tuxun-helper\tuxun_proxy.py)）**：
`anti_decoy, near_m, decoy_window, display_delay, api_poll, ai_auto, oneclock_enabled, oneclock_score, oneclock_key, map_size, map_tiles, map_zoom, name_protect_enabled, amap_key, amap_js_key, overlay_enabled, mirror_enabled, open_index, log_history, proxy_port, mirror_port, mirror_port_geo, control_port`
（`name_protect_enabled` 不直接写，改走 `name_protect` 布尔分支。）

---

## 二、图寻原生 API（`https://tuxun.fun`）

> 图寻 API `requests` 会话使用 `trust_env=False` **国内直连**，不走系统代理（`TuxunAgent`，[tuxun_agent.py](file:///f:\.Selfmade\tuxun-helper\tuxun_agent.py)）。带 Cookie 的会话在构造时注入 `FriendReferer: base_url`。

### 2.1 直连 Agent 端点

| 方法 | 路径 | 参数 | 对应调用 | 用途/返回要点 |
|---|---|---|---|---|
| GET | `/api/v0/time/getTime` | — | 连通性探针 | 服务器时间戳 `{success,data:<ms>}`；无 Cookie 也 200 |
| GET | `/api/v0/user/getLoginTicket` | — | 二维码登录 | 生成活二维码的 ticket（换取 `loginByWXPublicCode`） |
| GET | `/api/v0/login/loginByWXPublicCode` | 登录确认后的回调参数 | 微信扫码登录 | 换会话；`Set-Cookie` 由镜像捕获落袋 |
| GET | `/api/v0/tuxun/getProfile` | — | `verify_login` 首选（[agent:126](file:///f:\.Selfmade\tuxun-helper\tuxun_agent.py#L126)） | 用户信息 → 取 `UID` |
| GET | `/api/get_profile` | — | `verify_login` 兜底 2（[agent:127](file:///f:\.Selfmade\tuxun-helper\tuxun_agent.py#L127)） | 旧版 profile |
| GET | `/api/v0/tuxun/history/listSelf` | `page, pageSize` | `verify_login` 兜底 3 + `get_history`（[agent:128/161](file:///f:\.Selfmade\tuxun-helper\tuxun_agent.py#L128)） | 历史对局列表；任一含 `userId` 即视为登录成功 |
| GET | `/api/v0/tuxun/solo/get` | `gameId` | `get_game_info` 首选（[agent:197](file:///f:\.Selfmade\tuxun-helper\tuxun_agent.py#L197)） | 对局原始数据；`rounds` 对局结束/进行中（统计）都含真实经纬 |
| GET | `/api/v0/tuxun/game/getContent` | `gameId` | `get_game_info` 兜底 | 对局数据回退端点 |
| GET | `/api/v0/tuxun/mapProxy/getQQPanoInfo` | `pano` | `get_pano_info(source='qq_pano')`（[agent:178](file:///f:\.Selfmade\tuxun-helper\tuxun_agent.py#L178)） | 腾讯全景元信息（含经纬） |
| GET | `/api/v0/tuxun/mapProxy/getPanoInfo` | `panoId` | `get_pano_info(source='google')`（[agent:180](file:///f:\.Selfmade\tuxun-helper\tuxun_agent.py#L180)） | 全景元信息 |
| GET | `/api/v0/tuxun/draw/checkDailyChallenge` | — | [main.py:445](file:///f:\.Selfmade\tuxun-helper\main.py#L445) | 每日挑战状态 |
| GET | `/api/v0/tuxun/draw/dailyChallenge` | — | [main.py:450](file:///f:\.Selfmade\tuxun-helper\main.py#L450) | 每日挑战内容 |
| GET | `/api/v0/tuxun/task/dailyLottery/status` | — | [main.py:458](file:///f:\.Selfmade\tuxun-helper\main.py#L458) | 每日抽奖状态 |
| POST | `/api/v0/tuxun/task/dailyLottery/draw` | — | [main.py:461](file:///f:\.Selfmade\tuxun-helper\main.py#L461) | 执行每日抽奖 |
| GET | `/api/v0/user/profile` | — | 镜像 Cookie 捕获触发点 | 登录后浏览器请求携带的新 Cookie 由此落入会话 |

**`solo/get` 响应结构（`get_current_round`，[agent:228](file:///f:\.Selfmade\tuxun-helper\tuxun_agent.py#L228)）**：
```jsonc
{ "data": { "state": "...", "player": { "totalScore": ... },
  "rounds": [ { "round": 1, "panoId": "...", "lat": 12.3456, "lng": 45.6789,
                "heading": 0.0, /* …评分字段 */ } ] } }
```
- `rounds[-1]` = 最新回合；`panoId` 非空表示已出图。
- **进行中的对局** `solo/get` 的 `rounds` 坐标即干净真值（积分赛已实测），这是"API 直读"通道的关键。

**错误语义（`get_game_info`）**：`404` → 游戏不存在/过期；`401/403` → Cookie 失效或**被 WAF 标记**（抛 `TuxunAPIError` 中文提示）；其它非 200 → 该端点跳过，试下一个端点，全失败抛错。

### 2.2 镜像转发行为与直连中继（`MirrorRewrite`，[tuxun_proxy.py:1054](file:///f:\.Selfmade\tuxun-helper\tuxun_proxy.py#L1054))

| 规则 | 说明 |
|---|---|
| `GET /api/*`，且路径不含 `/login/`，且为**真实 flow**（有 `client_conn`） | 镜像用自己的 `requests` 会话**直连上游**取回再填回 flow（`_relay_api_get`），旁路 mitmproxy 上游 TLS 被图寻 CDN 指纹识别产生的 403 |
| `GET /api/*` 中含 `/login/`，或 `POST` | **不中继**，继续走原镜像 —— 保证登录的 `Set-Cookie` 能被捕获并落袋 |
| 路径匹配 `^/api/` | 命中 `_RELAY_API_READ_ONLY`（默认放行所有 `/api/` GET，登录链路除外） |
| `/api/` 上游首返 403 | 置 `app._tuxun_flagged=True` 并 `logger.warning`（只读诊断，暴露于 `/state.tuxun_flagged`；[tuxun_proxy.py:1204](file:///f:\.Selfmade\tuxun-helper\tuxun_proxy.py#L1204)） |
| `/game/report`、`/game/check`、`/api/v3/games` | 仅记录请求格式日志（不含敏感值，[tuxun_proxy.py:1073](file:///f:\.Selfmade\tuxun-helper\tuxun_proxy.py#L1073)） |

**签名**：中继时携带当前会话 Cookie（`MirrorCookieJar.load()`，优先 `.env`、回落会话文件）与浏览器 UA；`Referer: https://tuxun.fun/`；出超时、失败回 `502 {"api relay failed"}`。

### 2.3 被动直读通道（`SoloApiReader`，[tuxun_proxy.py:2001](file:///f:\.Selfmade\tuxun-helper\tuxun_proxy.py#L2001)）

- **零额外请求**：从**浏览器自身**发出的 `solo/get` / `game/getContent` 响应里直接解析真实坐标（`/solo/get`、`/game/getContent` 路径判定在 [tuxun_proxy.py:409](file:///f:\.Selfmade\tuxun-helper\tuxun_proxy.py#L409)），不做主动轮询平台。
- 回合数据在回合内不变 → 浏览器每次请求读一次即可。
- 街景元数据出现**未知全景**时，可能客户端未刷新回合数据，才补拉一次 `solo/get`（`10s` 限速），符合只读红线。

### 2.4 已知访问约束（实测 2026-09-24）

- **传输级**：经镜像（mitmproxy 上游 TLS）转发的 `/api` 一律 403；普通 HTTP 客户端（requests / urllib）直连 `/api` 全 200 → 已有直连中继旁路。
- **凭证级**：携带**被 WAF 标记的 `fun_ticket`** 时，**连公开 `getTime` 也是 403**（同出口、无指纹、非镜像下实测）。此层**无法用代码绕过**，只能换取干净凭证 / 换号。

---

## 三、GeoGuessr 原生 API（`https://www.geoguessr.com`）

实现于 `game_sources.py` 的 `GeoGuessrSource`（走系统代理与否取决于环境；国内/数据中心 IP 有地缘 403，需代理环境）。

| 方法 | 路径 | 调用 | 用途 |
|---|---|---|---|
| GET | `/api/v3/profiles/me/` | [game_sources.py:221](file:///f:\.Selfmade\tuxun-helper\game_sources.py#L221)（指定尾斜杠） | 当前用户信息 → 登录校验/取 `uid` |
| GET | `/api/v3/profile` | [game_sources.py:221](file:///f:\.Selfmade\tuxun-helper\game_sources.py#L221) | 用户信息兜底 |
| GET | `/api/v4/feed/private` | [game_sources.py:239](file:///f:\.Selfmade\tuxun-helper\game_sources.py#L239) | 私人信息流（近 20 条历史对局，时间倒序） |
| GET | `/api/v3/games/{game_id}` | [game_sources.py:262](file:///f:\.Selfmade\tuxun-helper\game_sources.py#L262) | 对局详情；`rounds` 含各回合 `lat/lng/panoId`（进行中也有，用于复盘） |
| GET | `/api/v3/games` | 被动监听 | 竞猜类请求，仅记录格式 |

**`v3/games/{id}` 解析**（[game_sources.py:263](file:///f:\.Selfmade\tuxun-helper\game_sources.py#L263)）→ `RoundInfo{round_no, pano_id, lat, lng, heading}` 列表；镜像里由 `_emit_geo_rounds`（[tuxun_proxy.py:1270](file:///f:\.Selfmade\tuxun-helper\tuxun_proxy.py#L1270)）把 `rounds[-1]` 的经纬直接上屏（`trusted=True`，来源 `GeoGuessr·API直读`）。

> GeoGuessr 的上游 403 是地缘封锁，**不是本地 bug**，不应改镜像层规避（会弄坏本机正常用户）。

---

## 四、第三方 / 地图服务

### 4.1 腾讯街景瓦片（`pano_images.fetch_qq_equirect`，[pano_images.py:50](file:///f:\.Selfmade\tuxun-helper\pano_images.py#L50)）
- 拼表：`https://sv{0-3}.map.qq.com/tile?svid={pano_id}&level=2&x={x}&y={y}&from=web&ch=0`
- 主机在 `sv0..sv3` 间按 `(x+y)%4` 取模轮询；`8` 线程并发取瓦片再拼回 `4096×2048` 全景。
- `Referer: https://map.qq.com/`；单瓦片失败重试 `3` 次，连续性校验 `>500` 字节。

### 4.2 Google 街景缩略图（`TuxunAgent.get_thumbnail_url` / `download_image`）
- 上游 `streetviewpixels-pa.googleapis.com`，国内被墙，需 `PROXY_URL`（如 Clash `http://127.0.0.1:7890`）。

### 4.3 OSM 瓦片（本地 `/tiles/osm/...` 代理）
- 上游候选（动态优先记住最近成功源）：`tile.openstreetmap.org` → `tile.openstreetmap.de`
- 合规 UA `TuxunHelper/1.0 (...)`；磁盘缓存 `cache/tiles/{z}_{x}_{y}.png`；可走代理级联。
- 存在原因：OSM 官方政策拦截 WebView/应用类 UA（403），本地代理规避。

### 4.4 逆地理编码（`geocode.py`）
| 服务 | 端点 | 说明 |
|---|---|---|
| 高德（国内） | `https://restapi.amap.com/v3/geocode/regeo` | `extensions/roadlevel/poitype` 等参数；Key = `AMAP_KEY`（.env）或内置默认 Web 服务 Key；`.env` 的 `AMAP_JS_KEY` 用于前端 JS API |
| 百度 | `https://api.bigdatacloud.net/data/reverse-geocode-client` | 兜底（BD09/离线） |
| OSM Nominatim | `https://nominatim.openstreetmap.org/reverse` | 兜底逆编码 |
| 外链地图 | `uri.amap.com/marker`、`openstreetmap.org/?mlat=&mlon=`、`google.com/maps?q=` | 只生成链接，不请求 |

> 坐标转换 WGS84↔GCJ-02↔BD09 为**本地纯换算**，不触网。逆编码带 `_MIN_INTERVAL` 节流。

### 4.5 AI 视觉后端（`ai_client.py`）
- OpenAI 兼容（`OPENAI_BASE_URL/OPENAI_API_KEY/OPENAI_MODEL`，国内首选如 glm-4.5v）+ Google Gemini（`API_KEY/GEMINI_MODEL`，需配套 `PROXY_URL`）。
- 是对局定位的**第三对齐**，非必要前台；`AI_TIMEOUT` 可调。

---

## 五、端口与进程约定

| 端口 | 用途 |
|---|---|
| `18080` | 控制 API + 选择页（第一节全部路由） |
| `8001` | 图寻镜像（reverse → `tuxun.fun`） |
| `8002` | GeoGuessr 镜像（reverse → `geoguessr.com`） |
| `8080` | 本地拦截代理（regular/upstream，接管系统代理时生效） |

> 端口可在 `config.json` 覆盖；`MIRROR_SPECS` 定义各平台 origin / CDN / env_key / session_file / default_port。
> 单实例由**单一 mitmproxy master** 承载所有监听（`MirrorUnifiedAddon` 按客户端连入的本地端口分发）。

---

## 六、反作弊红线

1. **只读优先**：对外只发起 `GET` 读数据；`POST` 仅限平台自身业务所必要的登录 / 抽奖链路。
2. **被动取真值**：实时答案优先复用浏览器自身发出的响应（`SoloApiReader` / 镜像 `_emit_geo_rounds`），不额外高频探测。
3. **不伪装破解**：不改造平台 TLS 指纹、不灌包、不主动触碰被标记凭证。
4. **遇标记降级**：访问被判"被标记凭证 / 传输 403"时，优先换干净凭证 / 换号，而非在代码层面硬闯风控。

---

## 七、坐标获取 API 详解（字段级）

> 真值三级依赖：**① API 直读（被动，首选）→ ② 街景元数据拦截（备用）→ ③ AI 图片（第三通道）**。
> 诱饵只注入在 ②，因此实时真值一律优先 ①。所有来源最终归一成 `RoundInfo` 上屏 / 复盘。

### 7.0 统一归宿 `RoundInfo`（[game_sources.py:54](file:///f:\.Selfmade\tuxun-helper\game_sources.py#L54)）

| 字段 | 类型 | 含义 |
|---|---|---|
| `round_no` | int | 回合编号 |
| `pano_id` | str | 全景 ID（GeoGuessr 已解码为真实 Google Pano ID） |
| `source` | str | `google_pano` / `qq_pano` / `baidu_pano` / `unknown` |
| `coord_sys` | str | `wgs84` / `gcj02` / `bd09` ← 由 `source` 映射 |
| `heading` | float? | 初始朝向（度） |
| `lat` / `lng` | float? | 真实经纬（在所选坐标系下） |

图寻 `source→coord_sys` 映射 `_TUXUN_COORD_SYS`（[game_sources.py:23](file:///f:\.Selfmade\tuxun-helper\game_sources.py#L23)）：
`google_pano→wgs84`、`qq_pano→gcj02`、`baidu_pano→bd09`。
`SoloApiReader.COORD_SYS`（[tuxun_proxy.py:2010](file:///f:\.Selfmade\tuxun-helper\tuxun_proxy.py#L2010)）与之等价。

---

### 7.1 图寻 `GET /api/v0/tuxun/solo/get?gameId=`（首选真值）

**用途**：获取整局原始数据；`rounds` 的坐标是**干净真值**（进行中对局也含，已实测）。这也是 `get_game_info` 的首选端点（失败回退 `game/getContent`，[tuxun_agent.py:197](file:///f:\.Selfmade\tuxun-helper\tuxun_agent.py#L197)）。

**响应字段（代码实际引用的 `data.*`）**：

| 路径 | 类型 | 含义 |
|---|---|---|
| `data.status` | str | 对局状态（`round`/`rank`/`ended` 等） |
| `data.players[]` | arr | 玩家数组；本局积分取 `players[*].score` |
| `data.rounds[]` | arr | 各回合；`rounds[-1]` = 最新回合 |
| `data.rounds[].round` | int | 回合号 |
| `data.rounds[].panoId` | str | 全景 ID（非空 = 已出图） |
| `data.rounds[].lat` / `.lng` | float | **真实经纬**（坐标系由 `source` 定） |
| `data.rounds[].heading` | float | 初始朝向 |
| `data.rounds[].source` | str | 图源 tag → 决定 `coord_sys` |
| `data.rounds[].move` | ? | 回合移动模式 → 喂给 `note_round_move` 判定"合法移动/同点" |

**实时解析 `SoloApiReader.note_rounds_response`（[tuxun_proxy.py:2028](file:///f:\.Selfmade\tuxun-helper\tuxun_proxy.py#L2028)）**：
1. 收集所有回合 `panoId` → `_known_panos`；
2. 取 `rounds[-1]`；有 `move` 则 `note_round_move`；`lat/lng` 非空则
   `handle_point(lat, lng, coord=COORD_SYS[source], source="图寻API直读", trusted=True)`；
3. 顺带 `maybe_ai_analyze(rd)`。

**复盘侧解析 `TuxunSource.get_game`（[game_sources.py:120](file:///f:\.Selfmade\tuxun-helper\game_sources.py#L120)）**：逐回合规整成 `RoundInfo`（`source=="google"` 归一为 `google_pano`）。

**触发入口**：①浏览器自身请求 `solo/get`/`game/getContent` 的响应被被动拦截（[tuxun_proxy.py:409](file:///f:\.Selfmade\tuxun-helper\tuxun_proxy.py#L409)）→ `note_rounds_response`；②街景元数据出现**未知全景**时 10s 限速补拉 `_fetch_once`（[tuxun_proxy.py:2065](file:///f:\.Selfmade\tuxun-helper\tuxun_proxy.py#L2065)，需 `TUXUN_COOKIE`）。

---

### 7.2 图寻 `mapProxy` 全景元数据（备用/非首选）

| 端点 | 参数 | `coord_sys` |
|---|---|---|
| `GET /api/v0/tuxun/mapProxy/getQQPanoInfo` | `pano=` | `gcj02`（腾讯） |
| `GET /api/v0/tuxun/mapProxy/getPanoInfo` | `panoId=` | `wgs84`（谷歌） |

返回 `data` 内含经纬等全景元信息（[tuxun_agent.py:171](file:///f:\.Selfmade\tuxun-helper\tuxun_agent.py#L171)）。
**注意**：这是"元数据层"，诱饵会注入在这里（`anti_decoy` 在此判定），故实时真值**优先 `solo/get`**，
此端点用于取某全景的网格/拼图基元，不作为首选真值。

---

### 7.3 GeoGuessr `GET /api/v3/games/{game_id}`

**响应字段（代码实际引用）**，`rounds[]` 各元素：
- `panoId`：**hex 编码的 ASCII**，经 `_decode_geo_pano`（[game_sources.py:38](file:///f:\.Selfmade\tuxun-helper\game_sources.py#L38)）解码为真实 Google Pano ID（`bytes.fromhex(pid).decode('ascii')`，非 20+ 位纯 hex 则原样）；
- `lat` / `lng`：**WGS84** 真值；
- `heading`：初始朝向；
- `round`：回合号；
- 顶层 `state`（对局状态）/ `player.totalScore`：新版为对象 `{amount, unit, …}`，取 `amount`（[game_sources.py:277](file:///f:\.Selfmade\tuxun-helper\game_sources.py#L279)）。

**实时镜像上屏 `_emit_geo_rounds`（[tuxun_proxy.py:1270](file:///f:\.Selfmade\tuxun-helper\tuxun_proxy.py#L1270)）**：当镜像命中 `…/api/v3/games` 的 JSON 响应时，取 `rounds[-1]` 的 `lat/lng/panoId` → `handle_point(…, source="GeoGuessr·API直读", trusted=True)`。
**复盘侧 `GeoGuessrSource.get_game`（[game_sources.py:261](file:///f:\.Selfmade\tuxun-helper\game_sources.py#L261)）**：同样规整为 `RoundInfo`。

---

### 7.4 Google GetMetadata 街景（拦截层，非首选真值）

`parse_google_metadata`（[tuxun_proxy.py:301](file:///f:\.Selfmade\tuxun-helper\tuxun_proxy.py#L301)）解析 Google GetMetadata 的 **protobuf-JSON** 内部结构：
1. 去前缀 `)]}'\r\n\t `；
2. `pano`：在 `data[0][0]` 或 `data[1][0][0]` 中取首个"8..64 位、非全数字"字符串；
3. `lat/lng`：`data[1][0][5][0][1][0]` 节点兼容 `(2,3)/(3,2)` 两种顺序（`|lat|≤90，|lng|≤180` 校验）；
4. 兜底：递归扫描 `_scan_latlng`。

返回 `(lat, lng, pano)`，WGS84。**只在拦截模式（接管 8080 代理）触发**；镜像模式无法走到这层，纯靠 7.1 的 `solo/get`。**诱饵也注入在这层**，`anti_decoy` 依据距离变化/回合移动模式排除。

---

### 7.5 全景拼图（供 AI / 复盘，非实时）

| 图源 | 通道 | 输出 | 备注 |
|---|---|---|---|
| 腾讯 | `fetch_qq_equirect` 瓦片 `sv{0-3}.map.qq.com/tile?svid=&level=2&x=&y=&from=web&ch=0` | 拼 `4096×2048` 全景（8 线程/重试 3） | 需 Pillow |
| 谷歌 | `get_thumbnail_url`（streetviewpixels） | 缩略图 | 需 `PROXY_URL` |

这些仅供 `ai_auto` / CLI 分析，**不参与实时原点/答案**。

---

### 7.6 参考系与"原点/答案"语义

- `handle_point(coord=…)` 记入的是**该图源坐标系**（wgs84/gcj02/bd09）；地图展示用瓦片源决定底图坐标系（`amap`=GCJ-02，`osm`/`arcgis`=WGS84），不同坐标系经 `geocode.py` 本地换算对齐。
- 悬浮窗的三个标记：**原点**（`origin`，回合初期真实落点，`trusted`）、**当前**（`current`，随合法移动更新）、**答案**（`answer`，最终提交）。诱饵只影响元数据层，API 直读的主点带 `trusted=True`。

---

## 八、附录：配置面 / 登录链路 / 被动通道

### 8.1 `config.json` 默认配置面（`DEFAULT_CONFIG`，[tuxun_proxy.py:103](file:///f:\.Selfmade\tuxun-helper\tuxun_proxy.py#L103)）

| 键 | 默认 | 说明 |
|---|---|---|
| `proxy_enabled` | false | 启动时是否自动接管系统代理 |
| `proxy_port` | 8080 | 本地拦截代理端口 |
| `display_delay` | 0.4 | 捕获到坐标后显示延迟（秒），缓解瞬间跳图 |
| `map_zoom` | 5 | 地图初始缩放 |
| `map_tiles` | amap | 瓦片源：`osm`（本地代理+合规UA+缓存）/ `amap` / `arcgis`；取值非法回退 `osm` |
| `amap_key` / `amap_js_key` | 内置默认 | 高德 Web 服务 / JS Key（可用 `.env` `AMAP_KEY` 覆盖） |
| `log_history` | true | 捕获点是否写入 `history.jsonl` |
| `upstream_proxy` | "" | 上级代理；留空自动跟随系统已有代理（Clash 自适应） |
| `anti_decoy` | true | 反作弊诱饵识别（距离判定 + 回合移动模式联动） |
| `decoy_window` | 3.0 | 距首个坐标多少秒内出现的不同坐标视为疑似诱饵 |
| `near_m` | 150 | 距锚点多少米内视为同一地点 |
| `mirror_port` | 8001 | 图寻镜像端口 |
| `control_port` | 18080 | 悬浮窗状态/设置 API 端口 |
| `mirror_enabled` | true | 启动自动开镜像（2.0 起默认） |
| `api_poll` | true | API 直读（解析浏览器自身 `solo/get`，被动零请求） |
| `ai_auto` | false | 新回合自动抓图分析并自动对答案 |
| `cookie_declined` | {tuxun:false,geoguessr:false} | 自动录入 Cookie 的"否"记忆 |
| `name_protect` | {enabled:false,rules:[]} | DOM 级替换页面昵称/ID（可自动学习） |
| `oneclock_*` | off / 3500 / F9 | 一键特定分数（热键按分数模型反推距离经 ws 提交） |
| `map_size` | 0 | 计分地图尺寸(km)；0=按回合自动（中国≈6120 / 世界≈14916） |
| `overlay_enabled` | true | 镜像页是否注入悬浮窗 |
| `open_index` | true | 启动后自动打开选择页 |

2.0 迁移标志：老配置首次加载时强制 `mirror_enabled=true` 并写回 `v2_mirror_migrated=true`。

### 8.2 登录落袋的三条路径（都写 `.env` + 热更新，值不入日志）

统一校验：图寻要求含 `fun_ticket=`、GeoGuessr 要求含 `session=`，否则拒绝；幂等（值未变化不重复写）。成功即置 `cookies_known[plat]=True`；图寻则额外 `api_reader.refresh_agent()` 立即更迭直连会话。

| 路径 | 入口 | 说明 |
|---|---|---|
| 官网官方登录 | `POST /official-login/{plat}` → `start_official_login` | 先接管系统代理拦截，让用户在官网登录，自动抓 Cookie |
| 镜像内登录 | 镜像 `Set-Cookie` → `_fix_set_cookie` → `note_mirror_login` | 登录发生在镜像页，Set-Cookie 被剥离 Domain/Secure 后落袋（2.0 主路径） |
| 手动粘入 | `POST /manual-cookie` → `save_manual_cookie` | 直接贴 Cookie 头，立即生效 |

### 8.3 `MIRROR_SPECS`（[tuxun_proxy.py:2795](file:///f:\.Selfmade\tuxun-helper\tuxun_proxy.py#L2795)）

| 平台 | origin | env_key | session_file | cdn_origin | port_key / 默认 |
|---|---|---|---|---|---|
| tuxun | `https://tuxun.fun` | `TUXUN_COOKIE` | `_mirror_session_tuxun.txt` | `https://b68res.daai.fun` | `mirror_port` / 8001 |
| geoguessr | `https://www.geoguessr.com` | `GEOGUESSR_COOKIE` | `_mirror_session_geo.txt` | — | `mirror_port_geo` / 8002 |

镜像统一由**单一 mitmproxy master**承载（`MirrorUnifiedAddon` 按客户端连入本地端口分发到 图寻 / Geo / 拦截 三套处理链），多 master 会互踩全局注册表导致 addon 不触发（需保持单 master 架构）。

### 8.4 websocket 被动通道（`TuxunInterceptor.websocket_message`，[tuxun_proxy.py:376](file:///f:\.Selfmade\tuxun-helper\tuxun_proxy.py#L376)）

- 捕获平台 `wss://.../ws`（host 含 `tuxun`）下发的对局消息，仅被动监听。
- 过滤心跳（`heart_beat`）；上下行方向都会记录（脱敏）。
- **积分赛**：下行 `"status":"rank"` 消息内含本回合真实 `lat/lng` 与 `panoId` → 直接作为可信主点 (`trusted=True`, 来源 `积分赛答案揭示`) 上屏。
- 对战/积分模式的状态与答案走 ws 下发，这条通道补齐了 HTTP API 之外的真值来源。

### 8.5 系统代理热切换（[tuxun_proxy.py:167](file:///f:\.Selfmade\tuxun-helper\tuxun_proxy.py#L167)）

- `set_system_proxy(enable, server="127.0.0.1:8080")`：Windows 通过注册表 `Internet Settings` 写 `ProxyEnable/ProxyServer/ProxyOverride`（local 直连名单：localhost/127.*/192.168.*/10.*/172.16-31.* 等）。
- `_resolve_upstream`（[tuxun_proxy.py:2567](file:///f:\.Selfmade\tuxun-helper\tuxun_proxy.py#L2567)）识别已有系统代理（Clash）作为上级代理，镜像/瓦片/直连复用，避免多代理打架。
- `enable_interception` / `restore_proxy`（[tuxun_proxy.py:2577/2616](file:///f:\.Selfmade\tuxun-helper\tuxun_proxy.py#L2577)）在挂代理与还原之间切换，退出时必还原。