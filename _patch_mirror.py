"""一次性补丁：把 MirrorRewrite 泛化为按平台配置（tuxun/geoguessr 双源）。"""
import re

P = "tuxun_proxy.py"
content = open(P, encoding="utf-8").read()

start = content.find("class MirrorRewrite:")
assert start > 0, "MirrorRewrite 未找到"
# 类块结束 = 其后第一个顶层 class/def
tail = content[start:]
m_end = re.search(r"\n(class |def )", tail[1:])
end = start + 1 + m_end.start()

NEW = '''class MirrorRewrite:
    """反向代理响应改写（按平台配置）：
    * 上游站点的绝对地址改写到本地镜像（JS 里转义斜杠的写法一并处理）；
    * 可选 CDN 改道（/cdn/ 前缀路由到静态资源站，仅图寻）；
    * Set-Cookie 去掉 Domain/Secure（镜像域为 127.0.0.1 + http）；
    * 页面注入悬浮窗（原点/目前/答案 + 设置），数据来自控制 API；
    * tuxun 源同时把流量委托给 TuxunInterceptor 做坐标捕获；
    * geoguessr 源解析对局 JSON，提取每回合真实坐标。
    """

    def __init__(self, app: "TuxunApp", local_port: int, interceptor: Optional["TuxunInterceptor"],
                 control_port: int, jar: "MirrorCookieJar", *, origin: str, origin_label: str,
                 kind: str, cdn_origin: str = ""):
        self.app = app
        self.local_port = local_port
        self.interceptor = interceptor
        self.jar = jar
        self.control_port = control_port
        self.origin = origin.rstrip("/")
        self.origin_host = self.origin.split("//")[1]
        self.origin_label = origin_label
        self.kind = kind
        self.cdn_origin = cdn_origin.rstrip("/")
        self._local = f"http://127.0.0.1:{local_port}"
        self._ws_local = f"ws://127.0.0.1:{local_port}"

    def request(self, flow: "http.HTTPFlow") -> None:
        # 竞猜/对局请求格式记录（兼容性分析；值不含敏感信息）
        try:
            u = flow.request.pretty_url
            if self.origin_host in u and ("/game/report" in u or "/game/check" in u or "/api/v3/games" in u):
                logger.info("对局/上报请求: %s", u[:400])
        except Exception:
            pass
        # CDN 资产改道（/cdn/ 前缀 -> cdn_origin）+ 上游会话注入
        try:
            if self.cdn_origin and flow.request.path.startswith("/cdn/"):
                flow.request.path = flow.request.path[len("/cdn"):]
                u = urlsplit(self.cdn_origin)
                flow.request.host = u.hostname
                flow.request.scheme = u.scheme
                flow.request.port = u.port or 443
            elif "127.0.0.1" in flow.request.pretty_host:
                ck = self.jar.load()
                if ck:
                    flow.request.headers["cookie"] = ck
        except Exception as exc:
            logger.debug("镜像请求改写失败: %s", exc)

    def _rewrite_text(self, text: str) -> str:
        # 平台前端把 API/静态资源地址硬编码在 JS/HTML 里，全部改写到本地镜像
        text = text.replace(self.origin, self._local)
        text = text.replace(self.origin.replace("/", "\\\\/"), self._local.replace("/", "\\\\/"))
        text = text.replace(f"wss://{self.origin_host}", self._ws_local)
        text = text.replace(f"wss:\\/\\/{self.origin_host}", self._ws_local)
        if self.cdn_origin:
            text = text.replace(self.cdn_origin, f"{self._local}/cdn")
        return text

    @staticmethod
    def _fix_set_cookie(headers) -> None:
        values = headers.get_all("set-cookie") if hasattr(headers, "get_all") else []
        if not values:
            return
        try:
            del headers["set-cookie"]
        except KeyError:
            pass
        import re as _re

        for v in values:
            v = _re.sub(r"Domain=[^;]+;?\\s*", "", v, flags=_re.I)
            v = _re.sub(r"Secure;?\\s*", "", v, flags=_re.I)
            headers.append("set-cookie", v)
        joined = "; ".join(values)
        if "fun_ticket=" in joined or "SESSION=" in joined or "session=" in joined:
            MirrorRewrite._cookie_store_update(joined)

    _cookie_store: dict = {}

    @staticmethod
    def _cookie_store_update(joined: str) -> None:
        MirrorRewrite._cookie_store["value"] = joined

    def response(self, flow: "http.HTTPFlow") -> None:
        # 坐标/对局捕获与主拦截完全一致（反向模式下 host 仍为上游域名）
        if self.kind == "tuxun" and self.interceptor is not None:
            self.interceptor.response(flow)
        if flow.response is None:
            return
        try:
            self._fix_set_cookie(flow.response.headers)
        except Exception as exc:
            logger.debug("Set-Cookie 改写失败: %s", exc)
        ctype = (flow.response.headers.get("content-type") or "").lower()
        if not any(t in ctype for t in ("text/html", "javascript", "json", "text/plain")):
            return
        try:
            text = flow.response.get_text(strict=False)
        except Exception:
            return
        if not text or len(text) > 8_000_000:
            return
        text = _META_CSP_RE.sub("", text)  # 页面内嵌 meta CSP 会拦注入脚本
        new = self._rewrite_text(text)
        injected = False
        if "text/html" in ctype and "</body>" in new.lower():
            pos = new.lower().rfind("</body>")
            injection = "<script>" + _OVERLAY_SCRIPT.replace(
                "__CONTROL_PORT__", str(self.app.config.get("control_port", 18080))
            ) + "</script>"
            new = new[:pos] + injection + new[pos:]
            injected = True
        if self.kind == "geoguessr" and "/api/v3/games" in flow.request.pretty_url:
            self._emit_geo_rounds(new)
        if new != text or injected:
            try:
                for h in ("content-security-policy", "content-security-policy-report-only"):
                    try:
                        del flow.response.headers[h]
                    except KeyError:
                        pass
                if injected:
                    flow.response.headers["cache-control"] = "no-store"
                flow.response.set_text(new)
            except Exception as exc:
                logger.debug("镜像改写失败: %s", exc)

    def _emit_geo_rounds(self, body: str) -> None:
        """GeoGuessr 对局 JSON：提取最新回合的答案坐标并上屏。"""
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            return
        rounds = data.get("rounds") if isinstance(data, dict) else None
        if not rounds:
            return
        rd = rounds[-1]
        lat, lng = rd.get("lat"), rd.get("lng")
        if lat is None or lng is None:
            return
        self.app.handle_point(
            float(lat), float(lng), coord="wgs84",
            source="GeoGuessr·API直读", pano=str(rd.get("panoId") or ""),
            trusted=True,
        )

    def websocket_message(self, flow: "http.HTTPFlow") -> None:
        if self.kind == "tuxun" and self.interceptor is not None:
            self.interceptor.websocket_message(flow)'''

content = content[:start] + NEW + content[end:]
open(P, "w", encoding="utf-8").write(content)
print("MirrorRewrite 已替换完成")
