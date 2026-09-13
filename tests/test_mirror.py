"""tests/test_mirror.py — 镜像模式单元测试（直接运行本文件）。"""
import sys, os, time, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import tuxun_proxy
from tuxun_proxy import TuxunApp, MirrorRewrite, ControlApiHandler, DEFAULT_CONFIG

tuxun_proxy.reverse_geocode = lambda *a, **k: "测试地址"  # 不触网
tuxun_proxy.save_config = lambda *a, **k: None  # 测试不写真实配置文件


class FakeHeaders(dict):
    def get_all(self, key):
        return [v for k, v in self.items() if k.lower() == key.lower()]

    def append(self, key, value):
        self[key] = value


class FakeResp:
    def __init__(self, t, ct="text/html"):
        self._t, self.headers = t, FakeHeaders({"content-type": ct})

    def get_text(self, strict=False):
        return self._t

    def set_text(self, t):
        self._t = t


class FakeReq:
    def __init__(self, url):
        from urllib.parse import urlsplit

        u = urlsplit(url)
        self.pretty_host = u.hostname or "127.0.0.1"
        self.pretty_url = url
        self.host = self.pretty_host
        self.scheme = u.scheme or "http"
        self.port = u.port or 443
        self.path = u.path + (("?" + u.query) if u.query else "")
        self.headers = FakeHeaders()
        self.pretty_host = self.pretty_host


class FakeFlow:
    def __init__(self, url, text, ct="text/html"):
        self.request = FakeReq(url)
        self.response = FakeResp(text, ct)


def make():
    cfg = dict(DEFAULT_CONFIG)
    cfg["display_delay"] = 0
    cfg["log_history"] = False
    app = TuxunApp(cfg, console_only=True)
    inter = tuxun_proxy.TuxunInterceptor(app)
    jar = tuxun_proxy.MirrorCookieJar(app, "TUXUN_COOKIE", "_mirror_session_tuxun.txt")
    rw = MirrorRewrite(app, 8001, inter, 18080, jar,
                       origin="https://tuxun.fun", origin_label="图寻",
                       kind="tuxun", cdn_origin="https://b68res.daai.fun")
    return app, rw


def test_rewrite():
    app, rw = make()
    page = ('<html><head><meta http-equiv="Content-Security-Policy" content="x"></head><body>'
            '<script src="https://b68res.daai.fun/tuxun/umi.abc.js"></script>'
            '<script src="https://tuxun.fun/api/bundle.js"></script>'
            '<script>var A="https://tuxun.fun/api";var B="wss://tuxun.fun/ws";'
            'var C="https:\\/\\/tuxun.fun\\/x";</script></body></html>')
    f = FakeFlow("https://tuxun.fun/", page)
    rw.response(f)
    new = f.response._t
    assert "https://tuxun.fun" not in new, f"原地址残留: {new[:200]}"
    assert "http://127.0.0.1:8001/api" in new, "本地 API 地址缺失"
    assert "ws://127.0.0.1:8001/ws" in new, "本地 ws 地址缺失"
    assert "b68res.daai.fun" not in new, "CDN 地址应改写到本地 /cdn/"
    assert "/cdn/tuxun/umi.abc.js" in new, "CDN 前缀缺失"
    assert "__TUXUN_OVERLAY__" in new, "悬浮窗未注入"
    assert "Content-Security-Policy" not in new, "meta CSP 未剥离"
    assert f.response.headers.get("cache-control") == "no-store", "注入页应禁缓存"
    print("1. URL 改写 + CDN 改道 + 悬浮窗注入 + CSP 剥离: 通过")


def test_request_retarget():
    app, rw = make()
    f = FakeFlow("http://127.0.0.1:8001/cdn/tuxun/umi.abc.js", "b")
    rw.request(f)
    assert f.request.host == "b68res.daai.fun" and f.request.path == "/tuxun/umi.abc.js", (
        f.request.host, f.request.path)
    f2 = FakeFlow("http://127.0.0.1:8001/api/v0/tuxun/solo/get?gameId=x", "b")
    f2.request.headers["cookie"] = ""
    rw.request(f2)
    assert f2.request.host == "127.0.0.1", "API 请求不应改道"
    print("5. CDN 改道 + API 会话注入: 通过")


def test_set_cookie():
    app, rw = make()
    f = FakeFlow("https://tuxun.fun/api/x", "{}", ct="application/json")
    f.response.headers["set-cookie"] = "SESSION=NEWVAL; Domain=.tuxun.fun; Path=/; Secure; HttpOnly"
    rw.response(f)
    sc = f.response.headers.get("set-cookie")
    assert "Domain=" not in sc, sc
    assert "Secure" not in sc, sc
    assert "NEWVAL" in sc, sc
    print("2. Set-Cookie 剥离: 通过 ->", sc)


def test_mirror_login_capture():
    """镜像登录捕获链：Set-Cookie -> jar 会话文件 -> .env 持久化。"""
    saved = {}
    tuxun_proxy.upsert_env_line = lambda k, v: saved.__setitem__(k, v)
    app, rw = make()
    f = FakeFlow("https://tuxun.fun/api/v0/login/loginByWXPublicCode", "{}", ct="application/json")
    f.response.headers["set-cookie"] = (
        "fun_ticket=TICKET123; Path=/; Domain=.tuxun.fun; Secure; HttpOnly; SameSite=None")
    rw.response(f)
    time.sleep(0.4)  # note_mirror_login 在后台线程执行
    assert saved.get("TUXUN_COOKIE") == "fun_ticket=TICKET123", saved
    assert os.path.isfile("_mirror_session_tuxun.txt"), "jar 会话文件未写入"
    with open("_mirror_session_tuxun.txt", encoding="utf-8") as fh:
        assert fh.read().strip() == "fun_ticket=TICKET123", "会话文件应只含 name=value 对"
    os.remove("_mirror_session_tuxun.txt")
    print("10. 镜像登录捕获（Set-Cookie -> jar -> .env）: 通过")


def test_location_rewrite():
    """登录回调 3xx：Location 指向上游域名应改写回本地镜像。"""
    app, rw = make()
    f = FakeFlow("https://tuxun.fun/api/v0/login/callback", "")
    f.response._t = ""
    f.response.headers["content-type"] = "text/html"
    f.response.headers["location"] = "https://tuxun.fun/?code=abc&state=x"
    rw.response(f)
    loc = f.response.headers.get("location")
    assert loc == "http://127.0.0.1:8001/?code=abc&state=x", loc
    # URL 编码变体（redirect_uri 参数）
    assert rw._rewrite_text("https%3A%2F%2Ftuxun.fun%2Fapi%2Fx") == \
        "http%3A%2F%2F127.0.0.1%3A8001%2Fapi%2Fx"
    print("11. Location 改写 + URL 编码变体: 通过")


def test_json_rewrite():
    app, rw = make()
    f = FakeFlow("https://tuxun.fun/api/v0/x", '{"u":"https://tuxun.fun/api"}', ct="application/json")
    rw.response(f)
    assert "127.0.0.1:8001" in f.response._t
    print("3. JSON 内地址改写: 通过")


def test_control_api():
    app, rw = make()
    ControlApiHandler._started = False          # 每个测试独立起服务
    ControlApiHandler.start(app, 18180, {})
    time.sleep(0.3)
    import urllib.request as rq
    http = rq.build_opener(rq.ProxyHandler({}))  # 绕过系统代理（Clash 等）

    state = json.loads(http.open("http://127.0.0.1:18180/state", timeout=5).read())
    assert state["origin"] is None and state["decoys"] == 0
    assert "oneclock_enabled" in state["settings"], "state 缺少一键分数设置"
    req = rq.Request(
        "http://127.0.0.1:18180/settings",
        data=json.dumps({"anti_decoy": False, "oneclock_score": 4200,
                         "oneclock_key": "F7", "name_protect": True}).encode(),
        headers={"Content-Type": "application/json"},
    )
    j = json.loads(http.open(req, timeout=5).read())
    assert j["settings"]["anti_decoy"] is False and app.config["anti_decoy"] is False
    assert j["settings"]["oneclock_score"] == 4200
    assert j["settings"]["oneclock_key"] == "F7"
    assert j["settings"]["name_protect"] is True and app.config["name_protect"]["enabled"] is True
    print("4. 控制 API state/settings（含一键分数/名称保护）: 通过")


def test_index_and_tutorial():
    app, rw = make()
    ControlApiHandler._started = False
    ControlApiHandler.start(app, 18181, {"tuxun": 8001})
    time.sleep(0.3)
    import urllib.request as rq
    http = rq.build_opener(rq.ProxyHandler({}))  # 绕过系统代理（Clash 等）

    idx = http.open("http://127.0.0.1:18181/", timeout=5).read().decode("utf-8")
    assert "TUXUN HELPER" in idx and "side-tuxun" in idx, "选择页内容异常"
    tut = http.open("http://127.0.0.1:18181/tutorial.md", timeout=5).read().decode("utf-8")
    assert "一键特定分数" in tut, "教程缺少一键分数章节"
    state = json.loads(http.open("http://127.0.0.1:18181/state", timeout=5).read())
    assert state["mirrors"] == {"tuxun": 8001}, state["mirrors"]
    print("6. 选择页 / + 教程 /tutorial.md + mirrors 端口状态: 通过")


def test_overlay_v2():
    app, rw = make()
    page = "<html><body>x</body></html>"
    f = FakeFlow("https://tuxun.fun/", page)
    rw.response(f)
    new = f.response._t
    for key in ("tx-setpanel", "oneclock_enabled", "WebSocket.prototype.send",
                '"scope":"tuxun"', "type: 'confirm'", "distForScore", "tx-ock"):
        assert key in new, f"悬浮窗 v2 缺少 {key}"
    print("7. 悬浮窗 v2：设置抽屉 + ws 钩子 + 一键分数: 通过")


def test_tiles_and_login_route():
    app, rw = make()
    ControlApiHandler._started = False
    ControlApiHandler.start(app, 18182, {})
    time.sleep(0.3)
    import urllib.request as rq
    http = rq.build_opener(rq.ProxyHandler({}))

    # 越界瓦片：不触网直接 502（校验路由与参数防护）
    try:
        http.open("http://127.0.0.1:18182/tiles/osm/30/0/0.png", timeout=5)
        raise AssertionError("越界瓦片应 502")
    except rq.HTTPError as e:
        assert e.code == 502, e.code
    # 非瓦片路径 404
    try:
        http.open("http://127.0.0.1:18182/tiles/osm/junk", timeout=5)
        raise AssertionError("垃圾路径应 404")
    except rq.HTTPError as e:
        assert e.code == 404, e.code
    # 登录触发端点（控制台无窗口，应返回可读提示而非崩溃）
    req = rq.Request("http://127.0.0.1:18182/login/tuxun", data=b"", method="POST")
    j = json.loads(http.open(req, timeout=8).read())
    assert j["status"] in ("error", "info", "success") and j.get("message"), j
    print("8. OSM 瓦片路由校验 + /login 端点: 通过")


def test_login_cookie_header():
    import http.cookiejar as cj
    c1 = cj.Cookie(0, "fun_ticket", "abc", None, False, "tuxun.fun", True, False,
                   "/", False, False, None, False, None, None, {})
    c2 = cj.Cookie(0, "session", "xyz", None, False, ".geoguessr.com", True, False,
                   "/", False, False, None, False, None, None, {})
    assert tuxun_proxy._cookies_header_for([c1, c2], "tuxun") == "fun_ticket=abc"
    assert tuxun_proxy._cookies_header_for([c1, c2], "geoguessr") == "session=xyz"
    c3 = cj.Cookie(0, "__cf_bm", "zzz", None, False, ".geoguessr.com", True, False,
                   "/", False, False, None, False, None, None, {})
    assert tuxun_proxy._cookies_header_for([c3], "geoguessr") == "", "无 session 应返回空"
    print("9. 登录 Cookie 提取与判定: 通过")


if __name__ == "__main__":
    test_rewrite()
    test_set_cookie()
    test_json_rewrite()
    test_request_retarget()
    test_control_api()
    test_index_and_tutorial()
    test_overlay_v2()
    test_tiles_and_login_route()
    test_login_cookie_header()
    test_mirror_login_capture()
    test_location_rewrite()
    print("== 镜像模式单元测试全部通过 ==")
