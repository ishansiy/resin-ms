# resin-ms

把上游 [Resinat/Resin](https://github.com/Resinat/Resin)（代理池 / 反向代理网关）做成**单端口**镜像，
用于部署在只暴露一个 HTTP 端口的托管平台（本仓库目标：魔塔社区国际版创空间 / ModelScope Studio）。

## 做了什么

上游是单二进制、默认监听 `2260`，面板 `/ui/`、管理 API `/api/v1/`、反代入口都在同一个端口；
但它的**额外 Endpoint 会各自监听独立端口**，而魔塔创空间只暴露 `7860`。所以这里加了一层 nginx：

```
对外 7860 ──┬─ /               落地页（200）
            ├─ /healthz        健康检查 → resin:2260
            ├─ /ui/            控制面板   → resin:2260
            ├─ /api/v1/...     管理 API   → resin:2260
            ├─ /<令牌>/<平台.账号>/<http|https>/<目标>   反向代理 → resin:2260
            └─ /e/1/ … /e/5/   额外 Endpoint → 127.0.0.1:30001…30005
```

- Resin 只监听 `127.0.0.1:2260`，公网请求一律经 nginx。
- **状态库就放在创空间持久盘**：`/mnt/workspace/resin-data/state/state.db`，重建容器/重新部署后配置不丢。
  持久盘是 OSSFS，SQLite 跑在 FUSE 上有页级损坏风险，所以启动时先 `PRAGMA quick_check` 自检，
  不通过就依次回滚 `state.db.bak1/bak2/bak3`，全坏则隔离损坏文件并以空库启动；
  运行中每 300s 做一次 `.backup` + `quick_check` 轮转（新备份校验不过就不覆盖旧备份）。
  持久盘不可写时自动降级到容器本地盘（服务能起，只是重建后丢配置）。
- 高写入的 `metrics.db` 与请求日志库留在容器本地盘（`/data`），避免持续写持久盘。
- 管理令牌支持三种投递方式：`X-Resin-Token` 头、`resin_admin` cookie（浏览器访问 `/t/<令牌>` 自动写入）、
  原生 `Authorization: Bearer`。后两者用于绕过托管平台边缘对 `Authorization` 的改写。

## 访问入口

| 入口 | 说明 |
|---|---|
| https://resin.007666.xyz | 自有域名（CF Worker `modelscope-gateway` 三件套：KV 路由 + 橙云 CNAME + Worker Route），浏览器导航不会 302 到平台页，cookie 为第一方 |
| https://hansiy-resin.ms.fun | 创空间原始地址（仅供直连排障） |

认证：面板登录与管理 API 都接受 `Authorization: Bearer <管理令牌>`；
经自有域名时 Worker 会把 Bearer 转写成 `X-Resin-Token`（ms.fun 边缘对带 Authorization 的请求一律 403），
镜像内 nginx 再映射回 Authorization 交给 Resin。因此三种写法都可用：

```bash
curl -H "Authorization: Bearer <管理令牌>"  https://resin.007666.xyz/api/v1/system/info   # 标准写法
curl -H "X-Resin-Token: <管理令牌>"          https://resin.007666.xyz/api/v1/system/info   # 直连被边缘拦截时的备用
curl -c jar https://resin.007666.xyz/t/<管理令牌>  # 浏览器：写 cookie 后直接进面板
```

⚠️ 反代路径请用**显式平台名**：`/<代理令牌>/<平台名>.<账号>/<https|http>/<目标地址>`。
上游 README 里的 `./`（不指定平台）占位段在 ms.fun 边缘/CF Worker 的 URL 规范化下会被吞掉，
表现为 `400 Failed to parse request URL`。

## IPPure 纯净度守护（内置后台任务）

镜像里带一个常驻脚本 `/opt/resin-pp/daemon.py`，由 `start.sh` 拉起，默认**每 3 小时**跑一轮：

1. **采集**：读本地节点表，对每个出口 IP 经 Resin 自己的反代入口（不同 account 名 → 落到不同节点）请求
   `https://my.ippure.com/v1/info`，拿到该出口的 `fraudScore`（风险系数，越低越纯净）、
   `isResidential`（是否原生/住宅）、`countryCode`。并发 10，单轮上限 420 次探测。
2. **缓存**：结果按 IP 落盘到持久盘 `/mnt/workspace/resin-data/purity/cache.json`，TTL 24 小时；
   `summary.json` 记录每轮评估数、平均风险系数、原生数量。
3. **自动分组**：按结果幂等维护 Resin platforms（平台名用中文；连续两轮无节点的托管平台会自动清理，用户手建的不动）：

| 平台 | 含义 | 依据 |
|---|---|---|
| `<中文国名>` | 按国家，如 `日本`、`美国`、`香港` | `region_filters=[cc]`（小写 ISO 两字母） |
| `住宅IP` | 原生/住宅 IP | tag 精确匹配 |
| `纯净` | 风险系数 ≤ 20 | 同上 |
| `良好` | 21 ~ 50 | 同上 |
| `高风险` | > 50 | 同上 |

> 坑：Resin 的 tag 正则匹配的是**完整候选串 `<订阅名>/<节点名>`**（等于 `display_tag`），
> 只写节点名匹配不到任何节点，平台会一直 503 `No available nodes for routing`。

可用环境变量（在创空间里配 Variable 即可覆盖）：`PP_INTERVAL`（秒，默认 10800）、
`PP_PROBE_MAX`（默认 420）、`PP_WORKERS`（默认 10）、`PP_CACHE_TTL`（默认 86400）。

用法示例：走日本节点 → `https://<域名>/<代理令牌>/日本/https/api.ipify.org?format=json`。

### 上游请求头契约（重要）

平台边缘（ms.fun / 阿里云 PAI-EAS 网关）会给发往容器的请求注入它自己的 `Authorization`，
Resin 会原样转给目标站点。因此镜像内 nginx 在反代路径上**重建**该头：

| 客户端发送 | 目标站点收到 |
|---|---|
| （不发） | 无 `Authorization` |
| `X-Resin-Auth: Bearer sk-xxx` | `Authorization: Bearer sk-xxx`（逐字节一致） |
| `Authorization: Bearer sk-xxx` | **被边缘覆盖，不可用**，请改用 `X-Resin-Auth` |
| 其它头（`xi-api-key`、`Cookie`、自定义） | 原样透传 |

```bash
# ElevenLabs 这类"同时有 xi-api-key 和 Authorization 就拒绝"的 API
curl -H "xi-api-key: $KEY" \
  "https://<域名>/<代理令牌>/美国/https/api.elevenlabs.io/v1/voices"
# 需要 Bearer 的 API
curl -H "X-Resin-Auth: Bearer $TOKEN" \
  "https://<域名>/<代理令牌>/美国/https/api.openai.com/v1/models"
```

`X-Resin-Auth` 会被 nginx 消费掉，不会传给目标站点。另外还剥掉了 `Original-Host`、
`Wl-Proxy-Client-Ip`、`Eagleeye-*`、`X-Fc-*` 这些内部链路头。

> `X-Resin-Account`（以及反代路径里的账号段）必须是 ASCII：中文会被 HTTP 头编码拒绝
> （`'latin-1' codec can't encode`）。平台名可以是中文，账号名不行。

⚠️ 平台名是中文，反代路径里的平台段**必须做百分号编码**（NFKC 无关，纯 URL 编码）：

```bash
PF=$(python3 -c 'import urllib.parse;print(urllib.parse.quote("日本"))')   # %E6%97%A5%E6%9C%AC
curl "https://resin.007666.xyz/<代理令牌>/$PF/https/api.ipify.org?format=json"
# → {"ip":"60.137.146.81"}
```

不支持自动编码的客户端，可改用 ISO 国名平台（旧 ASCII 名 `PP-JP` 已被清理）；需要 ASCII 别名时
把 `pp/daemon.py` 的 `COUNTRY_ZH` 改成 `f"{zh}-{cc.upper()}"` 之类的组合即可。

## 镜像标签

| 标签 | 含义 |
|---|---|
| `latest` | 最新叠加镜像（创空间引用的就是这个） |
| `vX.Y.Z` | 对应上游 release 的叠加镜像 |
| `app-vX.Y.Z` / `app-latest` | 纯上游镜像（无 nginx 层，回退排障用） |

## 自动更新

`.github/workflows/build.yml`：每天 03:17 CST 检查上游最新 release tag，GHCR 里没有对应镜像就
用上游官方 Dockerfile 重新编译 → 叠加 → 推送 → 调魔塔 deploy 接口重新部署并轮询到 `Running`。

所需配置：仓库 secret `MS_TOKEN`（魔塔 token）、仓库变量 `MS_STUDIO`（`属主/创空间名`）。

## 本地构建

```bash
docker build -f Dockerfile.overlay \
  --build-arg BASE=ghcr.io/ishansiy/resin-ms:app-latest \
  -t resin-ms:local .
docker run --rm -p 7860:7860 \
  -e RESIN_ADMIN_TOKEN=your-admin-token \
  -e RESIN_PROXY_TOKEN=your-proxy-token \
  resin-ms:local
```

依赖的平台 Secret / 环境变量：

| 变量 | 说明 |
|---|---|
| `RESIN_ADMIN_TOKEN` | 面板与管理 API 令牌；未提供则随机生成并打印在启动日志、写入持久盘 |
| `RESIN_PROXY_TOKEN` | 反向代理路径首段令牌；同上 |
| `RESIN_SNAPSHOT_INTERVAL` | 状态快照间隔秒数，默认 120 |
