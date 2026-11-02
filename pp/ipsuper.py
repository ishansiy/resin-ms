#!/usr/bin/env python3
"""IPSuper（https://ipsuper.com/?ip=<ip>）取数与解析。

背景：ipsuper 没有公开 REST 接口，前端用加密签名协议打
`POST https://api.ipsuper.com/endpoint`（头 xpa/xpq/xpw/xrd + 二进制 body），
无法用纯 HTTP 复刻。这里改用无头 Chrome 渲染页面、从 DOM 文本里解析字段。

与旧数据源 ippure 的差异（重要）：
- ippure 只能查「请求方自己的 IP」，必须把请求从每个节点代理出去；
- ipsuper 支持按任意 IP 查询，所以只要知道节点出口 IP 即可，无需代理绕行。
- ippure 给的是 fraudScore（越大越危险）；ipsuper 给的是「综合安全分」（越大越好）。
  本模块统一输出 risk = 100 - safe_score，保持下游语义不变。
"""

import os
import re
import shutil
import subprocess
import tempfile

CHROME_CANDIDATES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")

# 中文国家名 → ISO 代码（与 daemon.COUNTRY_ZH 互为反向）
COUNTRY_ISO = {
    "日本": "JP", "美国": "US", "香港": "HK", "台湾": "TW", "新加坡": "SG", "韩国": "KR",
    "德国": "DE", "法国": "FR", "荷兰": "NL", "英国": "GB", "西班牙": "ES", "波兰": "PL",
    "捷克": "CZ", "瑞典": "SE", "芬兰": "FI", "爱沙尼亚": "EE", "拉脱维亚": "LV",
    "立陶宛": "LT", "罗马尼亚": "RO", "保加利亚": "BG", "意大利": "IT", "爱尔兰": "IE",
    "奥地利": "AT", "瑞士": "CH", "比利时": "BE", "丹麦": "DK", "挪威": "NO",
    "葡萄牙": "PT", "希腊": "GR", "匈牙利": "HU", "斯洛伐克": "SK", "斯洛文尼亚": "SI",
    "克罗地亚": "HR", "塞尔维亚": "RS", "乌克兰": "UA", "俄罗斯": "RU", "土耳其": "TR",
    "以色列": "IL", "阿联酋": "AE", "沙特阿拉伯": "SA", "卡塔尔": "QA", "科威特": "KW",
    "巴林": "BH", "阿曼": "OM", "约旦": "JO", "黎巴嫩": "LB", "伊朗": "IR", "伊拉克": "IQ",
    "印度": "IN", "巴基斯坦": "PK", "孟加拉": "BD", "斯里兰卡": "LK", "尼泊尔": "NP",
    "印尼": "ID", "马来西亚": "MY", "泰国": "TH", "越南": "VN", "菲律宾": "PH",
    "柬埔寨": "KH", "缅甸": "MM", "老挝": "LA", "文莱": "BN", "蒙古": "MN",
    "哈萨克斯坦": "KZ", "乌兹别克斯坦": "UZ", "吉尔吉斯斯坦": "KG", "塔吉克斯坦": "TJ",
    "土库曼斯坦": "TM", "阿塞拜疆": "AZ", "亚美尼亚": "AM", "格鲁吉亚": "GE",
    "澳大利亚": "AU", "新西兰": "NZ", "加拿大": "CA", "墨西哥": "MX", "巴西": "BR",
    "阿根廷": "AR", "智利": "CL", "哥伦比亚": "CO", "秘鲁": "PE", "委内瑞拉": "VE",
    "厄瓜多尔": "EC", "玻利维亚": "BO", "巴拉圭": "PY", "乌拉圭": "UY", "圭亚那": "GY",
    "南非": "ZA", "埃及": "EG", "摩洛哥": "MA", "阿尔及利亚": "DZ", "突尼斯": "TN",
    "利比亚": "LY", "尼日利亚": "NG", "肯尼亚": "KE", "加纳": "GH", "埃塞俄比亚": "ET",
    "坦桑尼亚": "TZ", "乌干达": "UG", "塞内加尔": "SN", "科特迪瓦": "CI", "喀麦隆": "CM",
    "津巴布韦": "ZW", "赞比亚": "ZM", "莫桑比克": "MZ", "安哥拉": "AO", "博茨瓦纳": "BW",
    "纳米比亚": "NA", "毛里求斯": "MU", "中国": "CN", "澳门": "MO",
}

# 常见别名/全称 → ISO（ipsuper 的位置字段用中文全称）
COUNTRY_ALIAS = {
    "印度尼西亚": "ID", "阿拉伯联合酋长国": "AE", "大韩民国": "KR", "韩国": "KR",
    "俄罗斯联邦": "RU", "美利坚合众国": "US", "中国香港": "HK", "中国台湾": "TW",
    "中国澳门": "MO", "捷克共和国": "CZ", "越南社会主义共和国": "VN",
    "伊朗伊斯兰共和国": "IR", "土耳其共和国": "TR", "泰王国": "TH",
    "马来西亚联邦": "MY", "菲律宾共和国": "PH", "新加坡共和国": "SG",
    "大不列颠及北爱尔兰联合王国": "GB", "英格兰": "GB", "苏格兰": "GB",
    "荷兰王国": "NL", "德意志联邦共和国": "DE", "法兰西共和国": "FR",
}

RESIDENTIAL_WORDS = ("家宽", "住宅", "宽带", "移动网络", "移动")
IDC_WORDS = ("机房", "数据中心", "IDC", "云服务器", "托管")


def chrome_bin():
    for name in CHROME_CANDIDATES:
        p = shutil.which(name)
        if p:
            return p
    return None


def _strip_dom(html):
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<[^>]+>", "\n", html)
    html = html.replace("&nbsp;", " ").replace("&amp;", "&")
    lines = [x.strip() for x in html.split("\n")]
    return [x for x in lines if x]


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _after(lines, label, offset=1):
    """返回 label 之后第 offset 行；找不到返回 None。"""
    for i, line in enumerate(lines):
        if line == label and i + offset < len(lines):
            return lines[i + offset]
    return None


def parse(ip, html):
    """从渲染后的 DOM 中解析字段。返回 dict；解析失败 ok=False。"""
    lines = _strip_dom(html)
    text = " ".join(lines)
    out = {"ip": ip, "ok": False, "source": "ipsuper"}

    safe = grade = None
    if "越大越好" in lines:
        i = lines.index("越大越好")
        safe = _num(lines[i + 1]) if i + 1 < len(lines) else None
        if i + 4 < len(lines) and lines[i + 3] == "100":
            grade = lines[i + 4]
    if safe is None:
        m = re.search(r"综合安全分\s*(\d{1,3})\s*/\s*100", text)
        if m:
            safe = _num(m.group(1))
    if safe is None:
        return out

    out["safe_score"] = safe
    out["risk"] = max(0.0, min(100.0, 100.0 - safe))
    out["grade"] = grade

    threats = _after(lines, "威胁数目")
    kinds = _after(lines, "威胁种类")
    out["threats"] = int(_num(threats) or 0)
    out["threat_kinds"] = int(_num(kinds) or 0)

    out["location"] = _after(lines, "位置")
    iptype = _after(lines, "IP类型")
    out["ip_type"] = iptype
    usage = None
    if iptype:
        idx = lines.index(iptype)
        if idx + 1 < len(lines):
            usage = lines[idx + 1]
    out["usage_kind"] = usage

    asn_parts = []
    for i, line in enumerate(lines):
        if line == "ASN" and i + 1 < len(lines) and lines[i + 1].startswith("AS"):
            j = i + 1
            while j < len(lines) and lines[j] not in ("ASN", "流量", "网络构成比例（运营商业务范围）"):
                asn_parts.append(lines[j])
                j += 1
            break
    if asn_parts:
        out["asn"] = asn_parts[0].replace(" ", "")
        out["asn_org"] = " ".join(asn_parts[1:]).strip(" -()")

    m = re.search(r"人类流量\s*([\d.]+)%", text)
    out["human_pct"] = _num(m.group(1)) if m else None
    m = re.search(r"机器流量\s*([\d.]+)%", text)
    out["bot_pct"] = _num(m.group(1)) if m else None

    m = re.search(r"家宽网络\s*([\d.]+)%", text)
    out["home_net_pct"] = _num(m.group(1)) if m else None
    m = re.search(r"数据中心\s*([\d.]+)%", text)
    out["idc_net_pct"] = _num(m.group(1)) if m else None

    # 「使用类型」一行：按顺序给出各库判定（机房/家宽/移动…）
    votes = []
    if "使用类型" in lines:
        i = lines.index("使用类型") + 1
        stop = ("公司类型", "多源IP风险", "风险评分", "风险因子")
        while i < len(lines) and lines[i] not in stop:
            votes.append(lines[i])
            i += 1
    out["type_votes"] = votes
    out["res_votes"] = sum(1 for v in votes if any(w in v for w in RESIDENTIAL_WORDS))
    out["idc_votes"] = sum(1 for v in votes if any(w in v for w in IDC_WORDS))

    risks = [float(x) for x in re.findall(r"\b风险\s*([\d.]+)%", text)]
    if risks:
        out["max_risk"] = max(risks)
        labels = re.findall(r"风险\s*[\d.]+%\s*([\u4e00-\u9fa5]{2,4}风险|[\u4e00-\u9fa5]{2,4})", text)
        out["risk_labels"] = sorted(set(labels))[:8]

    cc = None
    if out["location"]:
        country = out["location"].split(",")[0].strip()
        cc = COUNTRY_ISO.get(country) or COUNTRY_ALIAS.get(country)
        if not cc:  # 兜底：全称里含已知国名（如「印度尼西亚共和国」）
            for name, iso in COUNTRY_ISO.items():
                if len(name) >= 2 and name in country:
                    cc = iso
                    break
    out["cc"] = cc
    # 数值字段必须长得像真的：ASN 形如 AS16509、国家可映射、使用类型非空且含常见词
    asn_ok = bool(re.match(r"^AS\d{1,10}$", str(out.get("asn") or "")))
    votes = out.get("type_votes") or []
    votes_ok = bool(votes) and any(
        any(w in v for w in RESIDENTIAL_WORDS + IDC_WORDS + ("未知", "企业", "教育")) for v in votes
    )
    if not asn_ok or not cc or not votes_ok or safe in (None, 0) and False:
        pass
    # 严格校验：只渲染出 SSR 外壳（XHR 未返回）时，页面里仍有静态的「越大越好 / 100」，
    # 会把安全分误读成 100 → 风险 0。没有位置/ASN/使用类型就一律判失败，交给兜底通道。
    if not asn_ok or not cc or not votes_ok:
        out["ok"] = False
        out["error"] = (
            f"unverified(asn={out.get('asn')!r} cc={cc!r} votes={votes[:3]!r} safe={safe!r})"
        )
        return out
    out["ok"] = True
    return out


NORMAL_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")


def fetch(ip, timeout=60, chrome=None, extra_wait_ms=11000, extra_args=None):
    """渲染 ipsuper 页面并返回原始 DOM。失败抛异常。"""
    bin_path = chrome or chrome_bin()
    if not bin_path:
        raise RuntimeError("找不到 chrome/chromium 可执行文件")
    profile = tempfile.mkdtemp(prefix="ipsuper-")
    args = [
        bin_path, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--enable-logging=stderr", "--log-level=0",
        "--disable-dev-shm-usage", "--no-first-run", "--disable-extensions",
        "--disable-background-networking", "--disable-sync", "--mute-audio",
        "--hide-scrollbars", "--blink-settings=imagesEnabled=false",
        f"--user-data-dir={profile}",
        f"--virtual-time-budget={extra_wait_ms}", "--dump-dom",
        f"https://ipsuper.com/?ip={ip}",
    ]
    if extra_args:
        args[1:1] = list(extra_args)
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout)
        dom = r.stdout.decode("utf-8", "replace")
        err = r.stderr.decode("utf-8", "replace")
        last_err = " | ".join([x for x in err.splitlines() if x.strip()][-3:])[:300]
        if not dom or len(dom) < 2000:
            raise RuntimeError(f"DOM 为空或过短 len={len(dom)} stderr={last_err}")
        fetch.last_stderr = last_err
        keep = [x for x in err.splitlines()
                if ("CONSOLE" in x or "ERR_" in x or "ERROR:" in x and "gl_display" not in x)]
        fetch.last_diag = " || ".join(keep[-8:])[:700]
        return dom
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def probe(ip, timeout=60, chrome=None, budget_ms=11000, extra_args=None, use_cdp=True):
    """查询单个 IP：优先 CDP 等数据渲染，失败再退回 --dump-dom；异常返回 ok=False。"""
    if use_cdp:
        try:
            dom = fetch_cdp(ip, timeout=max(timeout, 90), chrome=chrome,
                            extra_args=extra_args, data_wait=max(20, timeout // 2))
            res = parse(ip, dom)
            if res.get("ok"):
                res["via"] = "cdp"
                return res
            probe.last_cdp_error = res.get("error")
        except Exception as exc:  # noqa: BLE001
            probe.last_cdp_error = f"{type(exc).__name__}: {str(exc)[:120]}"
    try:
        dom = fetch(ip, timeout=timeout, chrome=chrome, extra_wait_ms=budget_ms,
                    extra_args=extra_args)
        res = parse(ip, dom)
        if not res.get("ok"):
            lines = _strip_dom(dom)
            probe_text = " / ".join(lines[:60])[:400]
            res.setdefault("dom_excerpt", probe_text)
            res["dom_len"] = len(dom)
            res["stderr"] = getattr(fetch, "last_stderr", "")[:200]
            if getattr(probe, "last_cdp_error", None):
                res["cdp_error"] = probe.last_cdp_error
            res["diag"] = getattr(fetch, "last_diag", "")
        return res
    except Exception as exc:  # noqa: BLE001
        return {"ip": ip, "ok": False, "source": "ipsuper", "error": str(exc)[:200]}


if __name__ == "__main__":
    import json
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "35.78.68.207"
    print(json.dumps(probe(target), ensure_ascii=False, indent=2))


class ProxyShim:
    """本地 CONNECT 转发：给浏览器一个带 Proxy-Authorization 的 HTTP 代理。

    Resin 的 2260 是正向代理，鉴权走 `Platform.Account:TOKEN` 的 Basic 头；
    Chrome 不接受 `--proxy-server` 里内联凭据，所以这里做一层本地转发补头。
    """

    def __init__(self, upstream="127.0.0.1", upstream_port=2260, auth="", port=0):
        import base64
        import socket
        import threading

        self.upstream = upstream
        self.upstream_port = upstream_port
        self.auth = "Basic " + base64.b64encode(auth.encode()).decode()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(128)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        import socket
        import threading

        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        import socket
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                head += chunk
            first = head.split(b"\r\n", 1)[0].decode("latin-1")
            parts = first.split()
            if len(parts) < 2:
                return
            method, target = parts[0], parts[1]
            up = socket.create_connection((self.upstream, self.upstream_port), timeout=20)
            if method.upper() == "CONNECT":
                req = (f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
                       f"Proxy-Authorization: {self.auth}\r\n\r\n")
                up.sendall(req.encode())
                resp = b""
                while b"\r\n\r\n" not in resp:
                    chunk = up.recv(4096)
                    if not chunk:
                        break
                    resp += chunk
                code = resp.split(b" ")[1] if b" " in resp else b"??"
                if not code.startswith(b"2"):
                    conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    return
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                rest = resp.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in resp else b""
                if rest:
                    conn.sendall(rest)
            else:
                headers = [h for h in head.split(b"\r\n") if h and not h.lower().startswith(b"proxy-")]
                up.sendall(b"\r\n".join(headers) + b"\r\nProxy-Authorization: " + self.auth.encode() + b"\r\n\r\n")
            self._pipe(conn, up)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass

    @staticmethod
    def _pipe(a, b):
        import select
        import socket
        try:
            while True:
                r, _, _ = select.select([a, b], [], [], 60)
                if not r:
                    break
                for s in r:
                    data = s.recv(65536)
                    if not data:
                        return
                    (b if s is a else a).sendall(data)
        finally:
            for s in (a, b):
                try:
                    s.close()
                except Exception:
                    pass

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except Exception:
            pass


# ---------------- CDP 渲染（推荐路径） ----------------
# --dump-dom + --virtual-time-budget 在部分 Chromium 版本上会在 XHR 返回前就落盘，
# 拿到的是 SSR 外壳（数值全是 "~"）。这里直接用 CDP：导航后轮询页面文本，
# 真正出数再取 outerHTML，超时才算失败。

def _ws_handshake(sock, host, path):
    import base64
    import os as _os
    key = base64.b64encode(_os.urandom(16)).decode()
    req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
           f"Sec-WebSocket-Version: 13\r\n\r\n")
    sock.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("WebSocket 握手断开")
        buf += chunk
    if b" 101 " not in buf.split(b"\r\n", 1)[0]:
        line0 = buf.split(b"\r\n", 1)[0]
        raise RuntimeError("WebSocket 握手失败：" + repr(line0[:80]))


def _ws_send(sock, text):
    import os as _os
    import struct
    data = text.encode()
    header = bytearray([0x81])
    n = len(data)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    mask = _os.urandom(4)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    sock.sendall(bytes(header) + masked)


def _ws_recv(sock, timeout=60):
    import struct
    sock.settimeout(timeout)
    payload = b""
    while True:
        head = b""
        while len(head) < 2:
            chunk = sock.recv(2 - len(head))
            if not chunk:
                raise RuntimeError("WebSocket 关闭")
            head += chunk
        opcode = head[0] & 0x0F
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", sock.recv(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", sock.recv(8))[0]
        data = b""
        while len(data) < length:
            chunk = sock.recv(length - len(data))
            if not chunk:
                raise RuntimeError("WebSocket 数据截断")
            data += chunk
        if opcode == 0x8:
            raise RuntimeError("WebSocket 主动关闭")
        if opcode in (0x9, 0xA):
            continue
        payload += data
        if head[0] & 0x80:
            return payload.decode("utf-8", "replace")


class _CDP:
    def __init__(self, ws_url, timeout=60):
        import socket as _sock
        import urllib.parse as _up
        u = _up.urlparse(ws_url)
        self.sock = _sock.create_connection((u.hostname, u.port), timeout=timeout)
        _ws_handshake(self.sock, f"{u.hostname}:{u.port}", u.path or "/")
        self.seq = 0

    def call(self, method, params=None, timeout=60):
        import json as _json
        self.seq += 1
        mid = self.seq
        _ws_send(self.sock, _json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = _json.loads(_ws_recv(self.sock, timeout=timeout))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} 失败：{msg['error']}")
                return msg.get("result") or {}

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def _devtools_port(profile, deadline):
    import os as _os
    import time as _time
    path = _os.path.join(profile, "DevToolsActivePort")
    while _time.time() < deadline:
        if _os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return int(f.readline().strip())
            except (OSError, ValueError):
                pass
        _time.sleep(0.2)
    return None


def fetch_cdp(ip, timeout=90, chrome=None, extra_args=None, data_wait=35):
    """用 CDP 打开页面，等数据真正渲染出来再取 outerHTML。"""
    import json as _json
    import subprocess
    import time as _time
    import urllib.request

    bin_path = chrome or chrome_bin()
    if not bin_path:
        raise RuntimeError("找不到 chrome/chromium 可执行文件")
    profile = tempfile.mkdtemp(prefix="ipsupercdp-")
    url = f"https://ipsuper.com/?ip={ip}"
    args = [
        bin_path, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--disable-dev-shm-usage", "--no-first-run", "--disable-extensions",
        "--disable-sync", "--mute-audio", "--hide-scrollbars",
        "--remote-debugging-port=0", f"--user-data-dir={profile}", url,
    ]
    if extra_args:
        args[1:1] = list(extra_args)
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = _time.time() + min(timeout, 30)
        port = _devtools_port(profile, deadline)
        if not port:
            raise RuntimeError("DevTools 端口未就绪")
        targets = []
        for _ in range(50):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as r:
                    targets = _json.loads(r.read().decode("utf-8", "replace"))
                targets = [t for t in targets if t.get("type") == "page"]
                if targets:
                    break
            except Exception:
                pass
            _time.sleep(0.2)
        if not targets:
            raise RuntimeError("没有可用的页面目标")
        cdp = _CDP(targets[0]["webSocketDebuggerUrl"], timeout=timeout)
        try:
            end = _time.time() + data_wait
            ready = False
            while _time.time() < end:
                res = cdp.call(
                    "Runtime.evaluate",
                    {"expression": "(() => { const t = document.body ? document.body.innerText : '';"
                                   "return JSON.stringify({n: t.length, has: t.indexOf('综合安全分') >= 0,"
                                   "ph: (t.match(/~/g) || []).length});})()",
                     "returnByValue": True},
                    timeout=15,
                )
                raw = (res.get("result") or {}).get("value") or "{}"
                st = _json.loads(raw)
                if st.get("has") and st.get("ph", 1) == 0 and st.get("n", 0) > 800:
                    ready = True
                    break
                _time.sleep(0.7)
            if not ready:
                raise RuntimeError("等待数据超时（页面仍为占位符）")
            res = cdp.call(
                "Runtime.evaluate",
                {"expression": "document.documentElement.outerHTML", "returnByValue": True},
                timeout=30,
            )
            dom = (res.get("result") or {}).get("value") or ""
            if len(dom) < 2000:
                raise RuntimeError(f"DOM 过短 len={len(dom)}")
            return dom
        finally:
            cdp.close()
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        shutil.rmtree(profile, ignore_errors=True)
