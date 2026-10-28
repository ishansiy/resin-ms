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
- 活跃 SQLite（`state.db`）跑在容器本地盘，每 120s 用 `sqlite3 .backup` + `quick_check` 做一致性快照到
  `/mnt/workspace/resin-data/`（OSSFS 上不直跑 SQLite），启动时自动恢复。
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
