#!/usr/bin/env python3
"""IPPure 纯净度守护进程（跑在创空间容器内，后台常驻）

做三件事：
  1. 采集：经 Resin 自己的反代入口，用不同 account 名轮询节点出口，
     调用 https://my.ippure.com/v1/info 拿到「该出口 IP」的
     fraudScore（风险系数，越低越纯净）、isResidential（是否原生/住宅）、countryCode。
  2. 落盘：结果按 IP 缓存到持久盘（含 TTL），供排查与复用。
  3. 分组：按国家 + IP 属性自动创建/维护 Resin platforms：
        PP-<CC>     按国家（region_filters）
        PP-RESI     原生/住宅 IP（按 tag 精确匹配）
        PP-PURE     风险系数 <= 20
        PP-GOOD     21 ~ 50
        PP-RISKY    > 50

只用标准库；任何异常都不影响主服务（失败只记日志，下个周期重试）。
"""
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

LOCAL_API = os.environ.get("RESIN_LOCAL_API", "http://127.0.0.1:2260")
PUBLIC_BASE = os.environ.get("PP_PUBLIC_BASE", "http://127.0.0.1:7860")
ADMIN = os.environ.get("RESIN_ADMIN_TOKEN", "")
PROXY = os.environ.get("RESIN_PROXY_TOKEN", "")
STATE_DIR = os.environ.get("PP_STATE_DIR", "/mnt/workspace/resin-data/purity")
INTERVAL = int(os.environ.get("PP_INTERVAL", "10800"))      # 3h
CACHE_TTL = int(os.environ.get("PP_CACHE_TTL", "86400"))    # 24h
PROBE_MAX = int(os.environ.get("PP_PROBE_MAX", "420"))      # 单轮最大探测请求数
WORKERS = int(os.environ.get("PP_WORKERS", "10"))           # 探测并发度
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


def load_cache():
    path = os.path.join(STATE_DIR, "cache.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, "cache.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def probe_one(account):
    """经反代指定 account，落到某个节点，问 IPPure 这个出口的属性。"""
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
        "ts": int(time.time()),
    }, None


def collect(cache, targets):
    """并发轮询不同 account，直到覆盖目标的出口 IP 或达到次数上限。"""
    missing = set(targets)
    if not missing:
        return 0
    hits = 0
    lock = threading.Lock()

    def worker(idx):
        nonlocal hits
        if not missing:
            return
        rec, err = probe_one(f"pp{idx:04d}")
        if err or not rec:
            return
        with lock:
            hits += 1
            cache[rec["ip"]] = rec
            missing.discard(rec["ip"])

    with futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(worker, range(1, PROBE_MAX + 1)))

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


def regroup(nodes, cache):
    """按国家 + 纯净度把节点分到平台（平台名用中文）。返回平台变更数。"""
    existing = existing_platforms()
    by_country = {}
    by_tier = {}
    for n in nodes:
        tag = node_tag(n)
        if not tag:
            continue
        region = (n.get("region") or "").lower()
        if region and region.isalpha() and len(region) == 2:
            by_country.setdefault(region, []).append(tag)
        rec = cache.get(n.get("egress_ip") or "")
        tier = tier_of(rec)
        if tier:
            by_tier.setdefault(tier, []).append(tag)

    desired = {}
    # 国家平台：靠 region_filters，天然跟随节点增减；名字显示中文
    for cc, tags in sorted(by_country.items()):
        name = COUNTRY_ZH.get(cc, cc.upper())
        desired[name] = {
            "name": name,
            "region_filters": [cc],
            "allocation_policy": "BALANCED",
            "reverse_proxy_miss_action": "TREAT_AS_EMPTY",
            "_desc": f"{cc.upper()} {len(tags)} 节点",
        }

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


def cycle():
    nodes = fetch_nodes()
    if not nodes:
        log("暂无节点，跳过本轮")
        return
    cache = load_cache()
    now = int(time.time())
    targets = {n["egress_ip"] for n in nodes
               if n.get("egress_ip") and (
                   n["egress_ip"] not in cache
                   or now - int(cache[n["egress_ip"]].get("ts", 0)) > CACHE_TTL)}
    log(f"节点 {len(nodes)} 个，待测出口 IP {len(targets)} 个（缓存 {len(cache)} 条）")
    hits = collect(cache, targets)
    save_cache(cache)

    made = regroup(nodes, cache)
    known = [cache.get(n.get("egress_ip") or "") for n in nodes]
    scored = [r for r in known if isinstance(r, dict)]
    nums = [r["score"] for r in scored if isinstance(r.get("score"), (int, float))]
    avg = round(sum(nums) / len(nums), 1) if nums else None
    resi = sum(1 for r in scored if r.get("residential"))
    if scored:
        log(f"本轮探测 {hits} 次；已评估 {len(scored)} 节点，平均风险系数 {avg}，"
            f"原生 {resi} 个；平台变更 {made} 个")
    else:
        log(f"本轮探测 {hits} 次，尚无评估结果")
    nudge_egress(nodes)
    summary = {
        "updated_at": now,
        "nodes": len(nodes),
        "evaluated": len(scored),
        "avg_score": avg,
        "residential": resi,
        "platforms_changed": made,
    }
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)


def main():
    if not ADMIN or not PROXY:
        log("缺少 RESIN_ADMIN_TOKEN / RESIN_PROXY_TOKEN，退出")
        return 1
    log(f"启动：每 {INTERVAL}s 一轮，缓存 TTL {CACHE_TTL}s，探测上限 {PROBE_MAX} 次/轮")
    time.sleep(90)  # 等 resin + nginx 起来
    while True:
        try:
            cycle()
        except Exception as exc:
            log(f"本轮异常：{type(exc).__name__}: {exc}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
