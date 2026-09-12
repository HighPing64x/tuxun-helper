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
    return app, MirrorRewrite(app, 8001, inter, 18080)


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


def test_json_rewrite():
    app, rw = make()
    f = FakeFlow("https://tuxun.fun/api/v0/x", '{"u":"https://tuxun.fun/api"}', ct="application/json")
    rw.response(f)
    assert "127.0.0.1:8001" in f.response._t
    print("3. JSON 内地址改写: 通过")


def test_control_api():
    app, rw = make()
    ControlApiHandler.start(app, 18180)
    time.sleep(0.3)
    import urllib.request as rq

    state = json.loads(rq.urlopen("http://127.0.0.1:18180/state", timeout=5).read())
    assert state["origin"] is None and state["decoys"] == 0
    req = rq.Request(
        "http://127.0.0.1:18180/settings",
        data=json.dumps({"anti_decoy": False}).encode(),
        headers={"Content-Type": "application/json"},
    )
    j = json.loads(rq.urlopen(req, timeout=5).read())
    assert j["settings"]["anti_decoy"] is False and app.config["anti_decoy"] is False
    print("4. 控制 API state/settings: 通过")


if __name__ == "__main__":
    test_rewrite()
    test_set_cookie()
    test_json_rewrite()
    test_request_retarget()
    test_control_api()
    print("== 镜像模式单元测试全部通过 ==")
