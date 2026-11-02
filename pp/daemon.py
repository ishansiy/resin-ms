#!/usr/bin/env python3
"""IP 纯净度守护进程（跑在创空间容器内，后台常驻）

做三件事：
  1. 采集：以 ipsuper（https://ipsuper.com/?ip=<ip>，无头 Chrome 渲染解析）为主源，
     按 Resin 给出的节点出口 IP 直接查「综合安全分 / 家宽或机房 / 国家 / ASN」，
     不必把请求从节点代理出去；ipsuper 查不到的再回退旧通道：
     经 Resin 反代轮询节点出口，调 https://my.ippure.com/v1/info 拿 fraudScore。
  2. 落盘：结果按 IP 缓存到持久盘（含 TTL），供排查与复用。
  3. 分组：按国家 + IP 属性自动创建/维护 Resin platforms（中文名）：
        <国家中文名>  按实测出口国家（region_filters + 精确 tag）
        住宅IP        家宽/住宅/移动，或多源判定家宽占多数
        纯净          风险分 <= 20（= 综合安全分 >= 80）
        良好          21 ~ 50
        高风险        > 50
        CDN出口       Cloudflare/Fastly/Akamai 等 Anycast 出口

只用标准库；任何异常都不影响主服务（失败只记日志，下个周期重试）。
"""
import ipaddress
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent import futures

try:
    import ipsuper
except Exception as _exc:  # 例如镜像里没装 chromium
    ipsuper = None

LOCAL_API = os.environ.get("RESIN_LOCAL_API", "http://127.0.0.1:2260")
PUBLIC_BASE = os.environ.get("PP_PUBLIC_BASE", "http://127.0.0.1:7860")
ADMIN = os.environ.get("RESIN_ADMIN_TOKEN", "")
PROXY = os.environ.get("RESIN_PROXY_TOKEN", "")
STATE_DIR = os.environ.get("PP_STATE_DIR", "/mnt/workspace/resin-data/purity")
INTERVAL = int(os.environ.get("PP_INTERVAL", "10800"))      # 3h
CACHE_TTL = int(os.environ.get("PP_CACHE_TTL", "86400"))    # 24h
PROBE_MAX = int(os.environ.get("PP_PROBE_MAX", "420"))      # 单轮最大探测请求数
WORKERS = int(os.environ.get("PP_WORKERS", "10"))           # IPPure 兜底并发度
BROWSER_WORKERS = int(os.environ.get("PP_BROWSER_WORKERS", "3"))   # ipsuper 无头浏览器并发度
IPSUPER_TIMEOUT = int(os.environ.get("PP_IPSUPER_TIMEOUT", "90"))  # 单个 IP 渲染超时
IPSUPER_ENABLE = os.environ.get("PP_IPSUPER", "1") != "0" and ipsuper is not None
IPPURE_FALLBACK = os.environ.get("PP_IPPURE_FALLBACK", "1") != "0"
REPROBE_FOREIGN = os.environ.get("PP_REPROBE_FOREIGN", "1") != "0"  # 非 ipsuper 来源的旧缓存当过期，一次性迁移
EGRESS_PROBE_MAX = int(os.environ.get("PP_EGRESS_PROBE_MAX", "25"))
IPPURE_URL = "https://my.ippure.com/v1/info"
TIER_PURE_MAX = 20
TIER_GOOD_MAX = 50
PLATFORM_PREFIX = "PP-"

# 平台名直接用中文（反代路径需对中文做百分号编码，已验证可用）
COUNTRY_ZH = {
    "ae": "阿联酋", "al": "阿尔巴尼亚", "am": "亚美尼亚", "ao": "安哥拉", "ar": "阿根廷",
    "at": "奥地利", "au": "澳大利亚", "az": "阿塞拜疆", "ba": "波黑", "bd": "孟加拉",
    "be": "比利时", "bg": "保加利亚", "bh": "巴林", "bn": "文莱", "bo": "玻利维亚",
    "br": "巴西", "bw": "博茨瓦纳", "by": "白俄罗斯", "ca": "加拿大", "ch": "瑞士",
    "ci": "科特迪瓦", "cl": "智利", "cm": "喀麦隆", "cn": "中国大陆", "co": "哥伦比亚",
    "cr": "哥斯达黎加", "cy": "塞浦路斯", "cz": "捷克", "de": "德国", "dk": "丹麦",
    "do": "多米尼加", "dz": "阿尔及利亚", "ec": "厄瓜多尔", "ee": "爱沙尼亚", "eg": "埃及",
    "es": "西班牙", "fi": "芬兰", "fr": "法国", "gb": "英国", "ge": "格鲁吉亚",
    "gh": "加纳", "gr": "希腊", "gt": "危地马拉", "hk": "香港", "hr": "克罗地亚",
    "hu": "匈牙利", "id": "印尼", "ie": "爱尔兰", "il": "以色列", "in": "印度",
    "iq": "伊拉克", "ir": "伊朗", "is": "冰岛", "it": "意大利", "jo": "约旦",
    "jp": "日本", "ke": "肯尼亚", "kh": "柬埔寨", "kr": "韩国", "kw": "科威特",
    "kz": "哈萨克斯坦", "la": "老挝", "lb": "黎巴嫩", "lk": "斯里兰卡", "lt": "立陶宛",
    "lu": "卢森堡", "lv": "拉脱维亚", "ma": "摩洛哥", "md": "摩尔多瓦", "me": "黑山",
    "mk": "北马其顿", "mm": "缅甸", "mn": "蒙古", "mo": "澳门", "mt": "马耳他",
    "mx": "墨西哥", "my": "马来西亚", "mz": "莫桑比克", "na": "纳米比亚", "ng": "尼日利亚",
    "nl": "荷兰", "no": "挪威", "np": "尼泊尔", "nz": "新西兰", "om": "阿曼",
    "pa": "巴拿马", "pe": "秘鲁", "ph": "菲律宾", "pk": "巴基斯坦", "pl": "波兰",
    "pr": "波多黎各", "pt": "葡萄牙", "py": "巴拉圭", "qa": "卡塔尔", "ro": "罗马尼亚",
    "rs": "塞尔维亚", "ru": "俄罗斯", "sa": "沙特", "se": "瑞典", "sg": "新加坡",
    "si": "斯洛文尼亚", "sk": "斯洛伐克", "sn": "塞内加尔", "th": "泰国", "tn": "突尼斯",
    "tr": "土耳其", "tw": "台湾", "tz": "坦桑尼亚", "ua": "乌克兰", "ug": "乌干达",
    "us": "美国", "uy": "乌拉圭", "uz": "乌兹别克斯坦", "ve": "委内瑞拉", "vn": "越南",
    "za": "南非", "zm": "赞比亚", "zw": "津巴布韦",
}
TIER_ZH = {"RESI": "住宅IP", "PURE": "纯净", "GOOD": "良好", "RISKY": "高风险"}
CDN_PLATFORM = "CDN出口"
# Anycast/CDN 出口网段兜底（探测还没跑到时也能识别，防止漏进国家平台）
CDN_NETS = [
    "104.16.0.0/13", "104.24.0.0/14", "104.28.0.0/16", "108.162.192.0/18",
    "131.0.72.0/22", "141.101.64.0/18", "162.158.0.0/15", "172.64.0.0/13",
    "173.245.48.0/20", "188.114.96.0/20", "190.93.240.0/20", "197.234.240.0/22",
    "198.41.128.0/17",  # Cloudflare
    "151.101.0.0/16",   # Fastly
]
# 出口落在 Anycast/CDN 网段的 ASN：这类"国家"是边缘机房位置，不是真实落地，不进国家平台
CDN_ASNS = {
    13335,   # Cloudflare
    54113,   # Fastly
    20940,   # Akamai
    16625,   # Akamai
    60068,   # CDN77
    19551,   # Incapsula
}


def log(msg):
    print(f"[pp {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def http(url, method="GET", body=None, headers=None, timeout=30):
    data = None
    hdrs = {"User-Agent": "resin-ippure/1.0"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw) if raw else None
            except ValueError:
                return r.status, raw.decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except ValueError:
            return exc.code, raw.decode("utf-8", "ignore")


def api(path, method="GET", body=None):
    # 直连 127.0.0.1:2260 时应用只认 Authorization；
    # 走 nginx(7860) 时认 X-Resin-Token。两个都带，任一路径都通。
    return http(f"{LOCAL_API}{path}", method, body,
                {"X-Resin-Token": ADMIN, "Authorization": f"Bearer {ADMIN}"}, 30)


# ---------------- 采集 ----------------

def fetch_nodes():
    status, data = api("/api/v1/nodes?limit=1000")
    if status != 200 or not isinstance(data, dict):
        log(f"取节点失败 status={status}")
        return []
    return data.get("items") or []


def cache_record_valid(rec):
    """只认字段看得住的结果，历史上 --dump-dom 写进去的外壳垃圾一律丢弃。"""
    if not isinstance(rec, dict):
        return False
    if not rec.get("cc") or len(str(rec.get("cc"))) != 2:
        return False
    if not re.match(r"^AS\d{1,10}$", str(rec.get("asn") or "")):
        return False
    votes = rec.get("type_votes") or []
    if not votes or all(str(v).strip("~ ") == "" for v in votes):
        return False
    return True


def load_cache():
    path = os.path.join(STATE_DIR, "cache.json")
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}
    cache, dropped = {}, 0
    for ip, rec in raw.items():
        if cache_record_valid(rec):
            cache[ip] = rec
        else:
            dropped += 1
    if dropped:
        log(f"缓存清理：丢弃 {dropped} 条无效记录（无国家/ASN 不合规/使用类型为空）")
    return cache


def save_cache(cache):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, "cache.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def probe_via_resin(account):
    """经反代指定 account，落到某个节点，问 IPPure 这个出口的属性（兜底通道）。"""
    url = f"{PUBLIC_BASE}/{urllib.parse.quote(PROXY)}/Default.{account}/https/my.ippure.com/v1/info"
    try:
        status, data = http(url, timeout=25)
    except Exception as exc:
        return None, str(exc)
    if status != 200 or not isinstance(data, dict) or not data.get("ip"):
        return None, f"status={status}"
    return {
        "ip": data["ip"],
        "score": data.get("fraudScore"),
        "residential": bool(data.get("isResidential")),
        "broadcast": bool(data.get("isBroadcast")),
        "cc": (data.get("countryCode") or "").lower(),
        "asn": data.get("asn"),
        "org": data.get("asOrganization"),
        "source": "ippure",
        "ts": int(time.time()),
    }, None


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_ipsuper(rec):
    """把 ipsuper 的结果映射成守护内部统一格式。

    score 统一为「风险分」= 100 - 综合安全分（越大越危险），保持与旧 ippure 语义一致，
    这样 TIER_PURE_MAX/TIER_GOOD_MAX 阈值不用改。
    """
    risk = _as_float(rec.get("risk"))
    if risk is None:
        safe = _as_float(rec.get("safe_score"))
        risk = None if safe is None else 100.0 - safe
    votes_r = rec.get("res_votes") or 0
    votes_i = rec.get("idc_votes") or 0
    usage = rec.get("usage_kind") or ""
    residential = (
        ("家宽" in usage) or ("住宅" in usage) or ("移动" in usage)
        or (votes_r >= 3 and votes_r > votes_i)
    )
    return {
        "ip": rec.get("ip"),
        "score": risk,
        "safe_score": rec.get("safe_score"),
        "grade": rec.get("grade"),
        "residential": bool(residential),
        "usage_kind": usage,
        "res_votes": votes_r,
        "idc_votes": votes_i,
        "human_pct": rec.get("human_pct"),
        "bot_pct": rec.get("bot_pct"),
        "max_risk": rec.get("max_risk"),
        "cc": (rec.get("cc") or "").lower(),
        "asn": rec.get("asn"),
        "org": rec.get("asn_org"),
        "location": rec.get("location"),
        "source": "ipsuper",
        "ts": int(time.time()),
    }


def collect_ipsuper(cache, missing):
    """用 ipsuper 按「出口 IP」直查：不必把请求从节点代理出去。"""
    todo = sorted(missing)[:PROBE_MAX]
    if not todo:
        return 0, missing
    log(f"ipsuper 待查 {len(todo)} 个出口 IP（并发 {BROWSER_WORKERS}）")
    lock = threading.Lock()
    hits = 0

    errs = []
    samples = []

    def worker(ip):
        nonlocal hits
        rec = ipsuper.probe(ip, timeout=IPSUPER_TIMEOUT)
        if not rec.get("ok"):
            if len(errs) < 3:
                extra = ""
                if rec.get("dom_excerpt"):
                    extra = f" | dom_len={rec.get('dom_len')} | {rec.get('dom_excerpt')}"
                errs.append(f"{ip}: {rec.get('error')}{extra}")
            return
        norm = normalize_ipsuper(rec)
        if not norm.get("ip"):
            return
        with lock:
            cache[norm["ip"]] = norm
            hits += 1
            missing.discard(norm["ip"])
            if len(samples) < 3:
                samples.append(
                    f"{norm['ip']} safe={norm.get('safe_score')} risk={norm.get('score')} "
                    f"cc={norm.get('cc')} resi={norm.get('residential')} asn={norm.get('asn')}"
                )

    with futures.ThreadPoolExecutor(max_workers=BROWSER_WORKERS) as pool:
        list(pool.map(worker, todo))
    for line in samples:
        log(f"ipsuper 样本 {line}")
    for line in errs:
        log(f"ipsuper 失败 {line}")
    return hits, missing


def collect_ippure(cache, missing):
    """兜底：并发轮询不同 account，直到覆盖剩余目标的出口 IP。"""
    lock = threading.Lock()
    hits = 0

    def worker(idx):
        nonlocal hits
        with lock:
            if not missing:
                return
        rec, err = probe_via_resin(f"pp{idx:04d}")
        if err or not rec:
            return
        with lock:
            if rec["ip"] in missing:
                hits += 1
                missing.discard(rec["ip"])
            cache[rec["ip"]] = rec

    with futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(worker, range(1, PROBE_MAX + 1)))
    return hits, missing


def collect(cache, targets):
    """先用 ipsuper 按 IP 直查，剩余未覆盖的再用 IPPure 兜底。"""
    missing = set(targets)
    if not missing:
        return 0
    hits = 0
    if IPSUPER_ENABLE:
        got, missing = collect_ipsuper(cache, missing)
        hits += got
    if missing and IPPURE_FALLBACK:
        log(f"ipsuper 未覆盖 {len(missing)} 个，回退 IPPure 探测")
        got, missing = collect_ippure(cache, missing)
        hits += got
    if missing:
        log(f"本轮未覆盖 {len(missing)} 个出口 IP（下轮继续）")
    return hits



# ---------------- 分组 ----------------

def node_tag(node):
    """Resin 的 tag 过滤匹配的是完整候选串 "<订阅名>/<节点名>"，
    与 display_tag 完全一致，所以直接用整串，不能去掉前缀。"""
    return (node.get("display_tag") or "").strip()


def tier_of(rec):
    if not rec:
        return None
    if rec.get("residential"):
        return "RESI"
    score = rec.get("score")
    if not isinstance(score, (int, float)):
        return None
    if score <= TIER_PURE_MAX:
        return "PURE"
    if score <= TIER_GOOD_MAX:
        return "GOOD"
    return "RISKY"


def existing_platforms():
    """取全部平台（分页拉全，避免漏掉中文名导致重复创建 409）。"""
    items = []
    offset = 0
    while True:
        status, data = api(f"/api/v1/platforms?limit=500&offset={offset}")
        if status != 200:
            log(f"取平台列表失败 status={status}")
            break
        if isinstance(data, list):
            items = data
            break
        if not isinstance(data, dict):
            break
        page = data.get("items") or []
        items.extend(page)
        total = data.get("total") or len(items)
        if not page or len(items) >= total:
            break
        offset = len(items)
    return {p.get("name"): p for p in items if isinstance(p, dict)}


def ensure_platform(existing, name, payload):
    """按名字幂等创建/更新平台；只在字段变化时 PATCH。"""
    cur = existing.get(name)
    body = {k: v for k, v in payload.items() if not k.startswith("_")}
    if cur is None:
        status, data = api("/api/v1/platforms", "POST", body)
        if status in (200, 201):
            log(f"新建平台 {name}（{payload.get('_desc', '')}）")
            return True
        if status == 409:
            # 并发/列表滞后导致的重复创建，视为已存在，下一轮会正常 PATCH
            return False
        log(f"新建平台 {name} 失败 status={status} {str(data)[:160]}")
        return False
    changed = False
    for key in ("regex_filters", "region_filters"):
        want = payload.get(key) or []
        have = cur.get(key) or []
        if want != have:
            changed = True
    if not changed:
        return False
    body.pop("name", None)
    status, data = api(f"/api/v1/platforms/{cur['id']}", "PATCH", body)
    if status == 200:
        log(f"更新平台 {name}（{payload.get('_desc', '')}）")
        return True
    log(f"更新平台 {name} 失败 status={status} {str(data)[:160]}")
    return False


def managed_manifest_path():
    return os.path.join(STATE_DIR, "managed.json")


def load_manifest():
    try:
        with open(managed_manifest_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_manifest(data):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(managed_manifest_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def is_managed_name(name):
    """只清理本守护自己建的平台，绝不碰用户手建的（如 Default）。"""
    if name in TIER_ZH.values() or name in COUNTRY_ZH.values():
        return True
    if re.match(r"^PP-[A-Z0-9]{2,6}$", name):   # 旧前缀命名
        return True
    return bool(re.match(r"^[A-Z]{2}$", name))  # 旧裸国家码（映射缺失回退产生）


def cleanup_stale(existing, desired_names, manifest):
    """连续两轮不在期望集合里的托管平台才删除，避免区号瞬时空导致抖动。"""
    removed = 0
    for name, plat in existing.items():
        if name in desired_names or not is_managed_name(name):
            continue
        miss = int(manifest.get(name, {}).get("missed", 0)) + 1
        manifest[name] = {"missed": miss}
        if miss >= 2:
            status, _ = api(f"/api/v1/platforms/{plat['id']}", "DELETE")
            if status in (200, 204):
                log(f"清理空平台 {name}（连续 {miss} 轮无节点）")
                manifest.pop(name, None)
                removed += 1
    for name in list(manifest):
        if name in desired_names:
            manifest.pop(name, None)
    return removed


_CDN_NETWORKS = None


def is_cdn_ip(ip):
    """出口 IP 是否落在已知 CDN/Anycast 网段（探测未覆盖时兜底）。"""
    global _CDN_NETWORKS
    if not ip:
        return False
    if _CDN_NETWORKS is None:
        _CDN_NETWORKS = [ipaddress.ip_network(n) for n in CDN_NETS]
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _CDN_NETWORKS)


def regroup(nodes, cache):
    """按国家 + 纯净度把节点分到平台（平台名用中文）。返回平台变更数。"""
    existing = existing_platforms()
    by_country = {}
    by_tier = {}
    by_cdn = []
    for n in nodes:
        tag = node_tag(n)
        if not tag:
            continue
        rec = cache.get(n.get("egress_ip") or "")
        tier = tier_of(rec)
        if tier:
            by_tier.setdefault(tier, []).append(tag)
        # CDN/Anycast 出口：geo 报的是边缘机房，不能当真实国家用 → 单独收容
        if (rec and rec.get("asn") in CDN_ASNS) or is_cdn_ip(n.get("egress_ip")):
            by_cdn.append(tag)
            continue
        # 国家以「实测出口国家」为准（探测过就用实测，没探测过退回 Resin 的 region 猜测）
        cc = ((rec or {}).get("cc") or "").lower() or (n.get("region") or "").lower()
        if cc and cc.isalpha() and len(cc) == 2:
            by_country.setdefault(cc, []).append(tag)

    desired = {}
    # 国家平台：靠 region_filters，天然跟随节点增减；名字显示中文
    for cc, tags in sorted(by_country.items()):
        name = COUNTRY_ZH.get(cc, cc.upper())
        tags = sorted(set(tags))
        payload = {
            "name": name,
            "region_filters": [cc],
            "allocation_policy": "BALANCED",
            "reverse_proxy_miss_action": "TREAT_AS_EMPTY",
            "_desc": f"{cc.upper()} {len(tags)} 节点（实测出口）",
        }
        # region 只是粗筛（CF 等 CDN 出口会被 geo 判成边缘机房所在地），再用精确 tag 收敛
        if len(tags) <= 800:
            payload["regex_filters"] = [f"^{re.escape(t)}$" for t in tags]
        desired[name] = payload

    # 纯净度平台：靠 tag 精确匹配（Resin 只能按 tag/region 过滤）
    labels = {"RESI": "原生/住宅 IP", "PURE": f"风险系数<={TIER_PURE_MAX}",
              "GOOD": f"{TIER_PURE_MAX + 1}~{TIER_GOOD_MAX}", "RISKY": f">{TIER_GOOD_MAX}"}
    for tier, zh in TIER_ZH.items():
        tags = sorted(set(by_tier.get(tier, [])))
        if not tags:
            continue
        if len(tags) > 300:
            log(f"跳过 {zh}：节点数 {len(tags)} 超过 300，正则过大")
            continue
        desired[zh] = {
            "name": zh,
            "regex_filters": [f"^{re.escape(t)}$" for t in tags],
            "allocation_policy": "BALANCED",
            "reverse_proxy_miss_action": "TREAT_AS_EMPTY",
            "_desc": f"{labels[tier]}，{len(tags)} 节点",
        }

    cdn_tags = sorted(set(by_cdn))
    if cdn_tags:
        desired[CDN_PLATFORM] = {
            "name": CDN_PLATFORM,
            "regex_filters": [f"^{re.escape(t)}$" for t in cdn_tags],
            "allocation_policy": "BALANCED",
            "reverse_proxy_miss_action": "TREAT_AS_EMPTY",
            "_desc": f"CDN/Anycast 出口，{len(cdn_tags)} 节点",
        }

    made = 0
    for name, payload in desired.items():
        if ensure_platform(existing, name, payload):
            made += 1
    manifest = load_manifest()
    made += cleanup_stale(existing, set(desired), manifest)
    save_manifest(manifest)
    return made


def nudge_egress(nodes):
    """对还没有出口 IP 的节点触发一次 egress 探测，帮助补齐状态。"""
    todo = [n for n in nodes if not n.get("egress_ip")][:EGRESS_PROBE_MAX]
    for n in todo:
        h = n.get("node_hash")
        if not h:
            continue
        try:
            api(f"/api/v1/nodes/{urllib.parse.quote(h)}/actions/probe-egress", "POST", {})
        except Exception:
            pass
    if todo:
        log(f"已对 {len(todo)} 个缺出口 IP 的节点触发探测")


def cache_stale(rec, now):
    """判断某条缓存是否该重测。"""
    if not isinstance(rec, dict):
        return True
    ts = int(rec.get("ts") or 0)
    if now - ts > CACHE_TTL:
        return True
    # 数据源迁移：老 ippure 条目在 ipsuper 可用时一律重测，避免两套评分混用
    if IPSUPER_ENABLE and REPROBE_FOREIGN and rec.get("source") != "ipsuper":
        return True
    return False


def cycle():
    nodes = fetch_nodes()
    if not nodes:
        log("暂无节点，跳过本轮")
        return
    cache = load_cache()
    now = int(time.time())
    targets = {n["egress_ip"] for n in nodes
               if n.get("egress_ip") and cache_stale(cache.get(n["egress_ip"]), now)}
    log(f"节点 {len(nodes)} 个，待测出口 IP {len(targets)} 个（缓存 {len(cache)} 条）")
    hits = collect(cache, targets)
    save_cache(cache)

    made = regroup(nodes, cache)
    known = [cache.get(n.get("egress_ip") or "") for n in nodes]
    scored = [r for r in known if isinstance(r, dict)]
    nums = [r["score"] for r in scored if isinstance(r.get("score"), (int, float))]
    avg = round(sum(nums) / len(nums), 1) if nums else None
    resi = sum(1 for r in scored if r.get("residential"))
    src = {}
    for r in scored:
        key = r.get("source") or "ippure"
        src[key] = src.get(key, 0) + 1
    if scored:
        src_desc = "/".join(f"{k} {v}" for k, v in sorted(src.items()))
        log(f"本轮探测 {hits} 次；已评估 {len(scored)} 节点（{src_desc}），"
            f"平均风险分 {avg}（越低越干净），住宅/家宽 {resi} 个；平台变更 {made} 个")
    else:
        log(f"本轮探测 {hits} 次，尚无评估结果")
    nudge_egress(nodes)
    summary = {
        "updated_at": now,
        "nodes": len(nodes),
        "evaluated": len(scored),
        "avg_score": avg,
        "residential": resi,
        "sources": src,
        "platforms_changed": made,
    }
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)


def code_fingerprint():
    import hashlib
    h = hashlib.sha256()
    for f in (__file__, os.path.join(os.path.dirname(__file__), "ipsuper.py")):
        try:
            h.update(open(f, "rb").read())
        except OSError:
            h.update(b"missing")
    return h.hexdigest()[:12]


def netcheck():
    """容器内网络连通性探针：区分「网络被封」与「浏览器没渲染出结果」。"""
    import subprocess as sp
    def _get(url, method="GET", data=None):
        try:
            req = urllib.request.Request(url, data=data, method=method,
                                         headers={"User-Agent": "Mozilla/5.0 Chrome/126"})
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read(400)
                return f"{method} {url} -> {r.status} len={len(body)}"
        except urllib.error.HTTPError as e:
            return f"{method} {url} -> HTTP {e.code}"
        except Exception as exc:  # noqa: BLE001
            return f"{method} {url} -> {type(exc).__name__}: {str(exc)[:90]}"
    try:
        ver = sp.run([ipsuper.chrome_bin() or "chromium", "--version"],
                     capture_output=True, timeout=20).stdout.decode("utf-8", "replace").strip()
    except Exception as exc:  # noqa: BLE001
        ver = f"未知({exc})"
    log(f"浏览器版本：{ver}")
    log("网络探针 " + _get("https://ipsuper.com/"))
    log("网络探针 " + _get("https://ipsuper.com/_nuxt/Dwx16dn2.js"))
    log("网络探针 " + _get("https://api.ipsuper.com/endpoint", method="POST", data=b"{}"))


def selftest_js():
    """最小 JS 执行测试：内联脚本能否改写 DOM。"""
    import subprocess as sp
    import tempfile as tf
    bin_path = ipsuper.chrome_bin() if ipsuper else None
    if not bin_path:
        log("JS 自检：跳过（找不到浏览器）")
        return
    html = "<html><body><div id=x>BEFORE</div><script>document.getElementById('x').textContent='JSRAN';</script></body></html>"
    d = tf.mkdtemp(prefix="jstest-")
    path = os.path.join(d, "t.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    try:
        r = sp.run([bin_path, "--headless=new", "--disable-gpu", "--no-sandbox",
                    "--disable-dev-shm-usage", f"--user-data-dir={d}/prof",
                    "--virtual-time-budget=3000", "--dump-dom", f"file://{path}"],
                   capture_output=True, timeout=60)
        out = r.stdout.decode("utf-8", "replace")
        log(f"JS 自检：JSRAN={'JSRAN' in out} dom_len={len(out)}")
        if "JSRAN" not in out:
            log(f"JS 自检 DOM：{out[:200]}")
    except Exception as exc:  # noqa: BLE001
        log(f"JS 自检异常：{type(exc).__name__}: {exc}")


def selftest_ipsuper():
    global IPSUPER_ENABLE
    if not (IPSUPER_ENABLE and ipsuper):
        log("自检 ipsuper：跳过（未启用或模块缺失）")
        return
    # 参数对照：找出在 Alpine Chromium 下能真正拿到数据的那一组
    variants = (
        ("CDP", {}),
        ("dump-dom", {"use_cdp": False}),
    )
    for tag, kw in variants:
        try:
            r = ipsuper.probe("8.8.8.8", timeout=IPSUPER_TIMEOUT, **kw)
        except Exception as exc:  # noqa: BLE001
            log(f"自检[{tag}] 异常：{type(exc).__name__}: {exc}")
            continue
        log(
            f"自检[{tag}] ok={r.get('ok')} safe={r.get('safe_score')} cc={r.get('cc')} "
            f"asn={r.get('asn')} votes={(r.get('type_votes') or [])[:3]} via={r.get('via')} "
            f"err={r.get('error')} cdp_err={r.get('cdp_error')}"
        )
    try:
        rec = ipsuper.probe("8.8.8.8", timeout=IPSUPER_TIMEOUT)
        log(
            "自检 ipsuper：ok={} safe={} cc={} asn={} votes={} err={} dom_len={} stderr={}".format(
                rec.get("ok"), rec.get("safe_score"), rec.get("cc"), rec.get("asn"),
                (rec.get("type_votes") or [])[:3], rec.get("error"),
                rec.get("dom_len"), (rec.get("stderr") or "")[:160],
            )
        )
        if rec.get("dom_excerpt"):
            log(f"自检 ipsuper DOM 片段：{rec['dom_excerpt'][:300]}")
        if rec.get("diag"):
            log(f"自检 ipsuper 控制台/网络：{rec['diag'][:600]}")
        if not rec.get("ok"):
            IPSUPER_ENABLE = False
            log("自检失败 → 本轮起 ipsuper 关闭，全部走 IPPure 兜底")
    except Exception as exc:  # noqa: BLE001
        log(f"自检 ipsuper 抛异常：{type(exc).__name__}: {exc}")


def main():
    if not ADMIN or not PROXY:
        log("缺少 RESIN_ADMIN_TOKEN / RESIN_PROXY_TOKEN，退出")
        return 1
    log(f"启动：每 {INTERVAL}s 一轮，缓存 TTL {CACHE_TTL}s，探测上限 {PROBE_MAX} 次/轮")
    log(f"数据源：ipsuper={'开' if IPSUPER_ENABLE else '关'}（并发 {BROWSER_WORKERS}），"
        f"IPPure 兜底={'开' if IPPURE_FALLBACK else '关'}")
    log(f"代码指纹 {code_fingerprint()}（daemon.py + ipsuper.py）")
    time.sleep(90)  # 等 resin + nginx 起来
    netcheck()
    selftest_js()
    selftest_ipsuper()
    while True:
        try:
            cycle()
        except Exception as exc:
            log(f"本轮异常：{type(exc).__name__}: {exc}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
