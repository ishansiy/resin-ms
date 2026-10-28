#!/bin/sh
# Resin 单端口启动编排
#   - 状态库 state.db 放持久盘 /mnt/workspace（重建容器不丢配置）
#   - 持久盘是 OSSFS，SQLite 在 FUSE 上有页级损坏风险 → 启动自检 + 备份轮转 + 自动回滚
#   - 高写入的 metrics / 请求日志库留在容器本地盘，避免拖垮持久盘
#   - nginx 统一对外 7860
set -eu

DATA_DIR="${RESIN_LOCAL_DIR:-/data}"
PERSIST_DIR="${RESIN_PERSIST_DIR:-/mnt/workspace/resin-data}"
CACHE_DIR="$DATA_DIR/cache"
LOG_DIR="$DATA_DIR/log"
CRED_FILE="$PERSIST_DIR/credentials.env"
ROTATE_INTERVAL="${RESIN_ROTATE_INTERVAL:-300}"

log() { printf '[resin-start %s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

mkdir -p "$CACHE_DIR" "$LOG_DIR" "$PERSIST_DIR"

# ---------- 持久盘可用性探测：不可写就降级到本地盘，保证服务能起来 ----------
PERSIST_OK=0
if mkdir -p "$PERSIST_DIR/state" 2>/dev/null && : > "$PERSIST_DIR/.write_test" 2>/dev/null; then
  PERSIST_OK=1
  rm -f "$PERSIST_DIR/.write_test"
fi
if [ "$PERSIST_OK" = "1" ]; then
  STATE_DIR="$PERSIST_DIR/state"
  log "状态库目录 = $STATE_DIR（持久盘 /mnt/workspace，重建容器不丢配置）"
else
  STATE_DIR="$DATA_DIR/state"
  mkdir -p "$STATE_DIR"
  log "警告：持久盘 $PERSIST_DIR 不可写，状态库降级到本地盘 $STATE_DIR（重建后会丢配置）"
fi

# ---------- 凭据：平台 Secret 优先；否则复用持久盘上已生成的；再否则随机生成 ----------
read_cred() { sed -n "s/^$1=//p" "$CRED_FILE" 2>/dev/null | head -n1; }
if [ -s "$CRED_FILE" ]; then
  [ -n "${RESIN_ADMIN_TOKEN:-}" ] || RESIN_ADMIN_TOKEN="$(read_cred RESIN_ADMIN_TOKEN)"
  [ -n "${RESIN_PROXY_TOKEN:-}" ] || RESIN_PROXY_TOKEN="$(read_cred RESIN_PROXY_TOKEN)"
fi
GEN_ADMIN=""; GEN_PROXY=""
if [ -z "${RESIN_ADMIN_TOKEN:-}" ]; then
  RESIN_ADMIN_TOKEN="$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40)"
  GEN_ADMIN=1
fi
if [ -z "${RESIN_PROXY_TOKEN:-}" ]; then
  RESIN_PROXY_TOKEN="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 28)"
  GEN_PROXY=1
fi
if [ -n "$GEN_ADMIN$GEN_PROXY" ]; then
  if ( umask 077; printf 'RESIN_ADMIN_TOKEN=%s\nRESIN_PROXY_TOKEN=%s\n' \
        "$RESIN_ADMIN_TOKEN" "$RESIN_PROXY_TOKEN" > "$CRED_FILE.tmp" ) 2>/dev/null \
     && mv "$CRED_FILE.tmp" "$CRED_FILE"; then
    log "未提供平台 Secret，已随机生成令牌并写入 $CRED_FILE"
  else
    rm -f "$CRED_FILE.tmp" 2>/dev/null || true
    log "未提供平台 Secret，已随机生成令牌（持久盘不可写，仅本次有效）"
  fi
  log "管理令牌(AdminToken): $RESIN_ADMIN_TOKEN"
  log "代理令牌(ProxyToken): $RESIN_PROXY_TOKEN"
fi
export RESIN_ADMIN_TOKEN RESIN_PROXY_TOKEN

# ---------- 状态库完整性：SQLite 跑在 OSSFS 上，先自检再决定用哪个文件 ----------
DB="$STATE_DIR/state.db"

db_ok() {
  [ -s "$1" ] || return 1
  [ "$(sqlite3 "$1" 'PRAGMA quick_check;' 2>/dev/null)" = "ok" ]
}

if [ -s "$DB" ]; then
  if db_ok "$DB"; then
    log "state.db 完整性检查通过（$(wc -c < "$DB") 字节）"
  else
    log "警告：state.db 完整性检查未通过，尝试回滚到最近可用备份"
    for f in state.db.bak1 state.db.bak2 state.db.bak3; do
      if db_ok "$STATE_DIR/$f"; then
        cp "$STATE_DIR/$f" "$DB" && log "已回滚到 $f"
        break
      fi
    done
    if ! db_ok "$DB"; then
      mv "$DB" "$STATE_DIR/state.db.corrupt.$(date +%s)" 2>/dev/null || rm -f "$DB"
      log "所有备份均不可用：已隔离损坏库，将以空库启动（损坏文件保留在 $STATE_DIR）"
    fi
  fi
else
  log "未发现 state.db，将初始化新库"
fi

# ---------- 备份轮转：只在 quick_check 通过时才替换备份，避免用坏库覆盖好备份 ----------
rotate() {
  [ -s "$DB" ] || return 0
  tmp="$DATA_DIR/.state.rot.$$"
  if ! sqlite3 "$DB" ".backup '$tmp'" >/dev/null 2>&1; then
    rm -f "$tmp"
    log "备份失败（库忙或不可读），本轮跳过"
    return 0
  fi
  if [ "$(sqlite3 "$tmp" 'PRAGMA quick_check;' 2>/dev/null)" = "ok" ]; then
    [ -s "$STATE_DIR/state.db.bak2" ] && mv "$STATE_DIR/state.db.bak2" "$STATE_DIR/state.db.bak3"
    [ -s "$STATE_DIR/state.db.bak1" ] && mv "$STATE_DIR/state.db.bak1" "$STATE_DIR/state.db.bak2"
    mv "$tmp" "$STATE_DIR/state.db.bak1"
    log "state.db 备份轮转完成（bak1=$(wc -c < "$STATE_DIR/state.db.bak1") 字节）"
  else
    rm -f "$tmp"
    log "警告：新备份 quick_check 未通过，保留旧备份不动"
  fi
}

# ---------- 启动 Resin（仅回环，公网走 nginx） ----------
export RESIN_STATE_DIR="$STATE_DIR" \
       RESIN_CACHE_DIR="$CACHE_DIR" \
       RESIN_LOG_DIR="$LOG_DIR" \
       RESIN_LISTEN_ADDRESS="${RESIN_LISTEN_ADDRESS:-127.0.0.1}" \
       RESIN_PORT="${RESIN_PORT:-2260}"

chown -R resin:resin "$STATE_DIR" "$CACHE_DIR" "$LOG_DIR" 2>/dev/null || true

log "启动 resin（监听 ${RESIN_LISTEN_ADDRESS}:${RESIN_PORT}）"
su-exec resin:resin /usr/local/bin/resin &
RESIN_PID=$!

i=0
while [ "$i" -lt 60 ]; do
  if curl -fsS "http://127.0.0.1:${RESIN_PORT}/healthz" >/dev/null 2>&1; then
    log "resin 就绪（/healthz OK）"
    break
  fi
  kill -0 "$RESIN_PID" 2>/dev/null || { log "resin 进程已退出，查看上方日志"; exit 1; }
  i=$((i + 5)); sleep 5
done

# ---------- 启动 nginx（对外 7860） ----------
nginx -c /etc/resin/nginx.conf -g 'daemon off;' &
NGX_PID=$!
log "nginx 已启动：对外 7860 → 面板 /ui/ ，API /api/ ，反代 /<令牌>/... ，额外端点 /e/1../e/5"

# 后台备份轮转
(
  while sleep "$ROTATE_INTERVAL"; do rotate; done
) &
ROT_PID=$!

# 后台 IPPure 纯净度守护：按国家/风险系数自动维护 platforms
# 输出直接进容器日志（便于平台侧排查），状态落持久盘 $PERSIST_DIR/purity/
PP_INTERVAL="${PP_INTERVAL:-10800}"
(
  PP_STATE_DIR="$PERSIST_DIR/purity" \
  PP_INTERVAL="$PP_INTERVAL" \
  PP_PUBLIC_BASE="http://127.0.0.1:7860" \
  RESIN_LOCAL_API="http://127.0.0.1:${RESIN_PORT:-2260}" \
  exec python3 /opt/resin-pp/daemon.py
) &
PP_PID=$!
log "IPPure 守护已启动（每 ${PP_INTERVAL}s 一轮 → 自动维护 PP-<国家> 与 PP-{RESI,PURE,GOOD,RISKY} 平台）"

cleanup() {
  trap - TERM INT EXIT
  log "收到退出信号，收尾中"
  kill -QUIT "$NGX_PID" 2>/dev/null || true
  kill -TERM "$RESIN_PID" 2>/dev/null || true
  wait "$RESIN_PID" 2>/dev/null || true
  kill "$ROT_PID" 2>/dev/null || true
  kill "$PP_PID" 2>/dev/null || true
  rotate
  exit 0
}
trap cleanup TERM INT

while kill -0 "$RESIN_PID" 2>/dev/null && kill -0 "$NGX_PID" 2>/dev/null; do
  sleep 5
done
log "子进程退出，整体退出"
cleanup
