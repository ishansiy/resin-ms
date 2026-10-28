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
    """display_tag 形如 '<订阅名>/<节点名>'，取节点名交给正则匹配。"""
    tag = (node.get("display_tag") or "").strip()
    if "/" in tag:
        tag = tag.split("/", 1)[1]
    return tag.strip()


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
    status, data = api("/api/v1/platforms")
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("items") or []
    else:
        items = []
    if status != 200:
        log(f"取平台列表失败 status={status}")
        return {}
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


def regroup(nodes, cache):
    """按国家 + 纯净度把节点分到平台。返回平台数。"""
    existing = existing_platforms()
    made = 0

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

    # 国家平台：靠 region_filters，天然跟随节点增减
    for cc, tags in sorted(by_country.items()):
        payload = {
            "name": f"{PLATFORM_PREFIX}{cc.upper()}",
            "region_filters": [cc],
            "allocation_policy": "BALANCED",
            "reverse_proxy_miss_action": "TREAT_AS_EMPTY",
            "_desc": f"{cc.upper()} 共 {len(tags)} 节点",
        }
        if ensure_platform(existing, payload["name"], payload):
            made += 1

    # 纯净度平台：靠 tag 精确匹配（Resin 只能按 tag/region 过滤）
    labels = {"RESI": "原生/住宅 IP", "PURE": f"风险系数<={TIER_PURE_MAX}",
              "GOOD": f"{TIER_PURE_MAX + 1}~{TIER_GOOD_MAX}", "RISKY": f">{TIER_GOOD_MAX}"}
    for tier, labels_txt in labels.items():
        tags = sorted(set(by_tier.get(tier, [])))
        if not tags:
            continue
        if len(tags) > 300:
            log(f"跳过 {tier}：节点数 {len(tags)} 超过 300，正则过大")
            continue
        payload = {
            "name": f"{PLATFORM_PREFIX}{tier}",
            "regex_filters": [f"^{re.escape(t)}$" for t in tags],
            "allocation_policy": "BALANCED",
            "reverse_proxy_miss_action": "TREAT_AS_EMPTY",
            "_desc": f"{labels_txt}，{len(tags)} 节点",
        }
        if ensure_platform(existing, payload["name"], payload):
            made += 1

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
