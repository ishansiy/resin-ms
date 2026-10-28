#!/bin/sh
# Resin 单端口启动编排：本地盘跑活跃 SQLite + 定时一致性快照落持久盘 + nginx 统一 7860
set -eu

DATA_DIR="${RESIN_LOCAL_DIR:-/data}"
PERSIST_DIR="${RESIN_PERSIST_DIR:-/mnt/workspace/resin-data}"
STATE_DIR="$DATA_DIR/state"
CACHE_DIR="$DATA_DIR/cache"
LOG_DIR="$DATA_DIR/log"
CRED_FILE="$PERSIST_DIR/credentials.env"
SNAP_DB="$PERSIST_DIR/state.db"
SNAP_PREV="$PERSIST_DIR/state.db.prev"
SNAP_INTERVAL="${RESIN_SNAPSHOT_INTERVAL:-120}"

log() { printf '[resin-start %s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

mkdir -p "$STATE_DIR" "$CACHE_DIR" "$LOG_DIR" "$PERSIST_DIR"

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
  umask 077
  {
    echo "RESIN_ADMIN_TOKEN=$RESIN_ADMIN_TOKEN"
    echo "RESIN_PROXY_TOKEN=$RESIN_PROXY_TOKEN"
  } > "$CRED_FILE.tmp"
  mv "$CRED_FILE.tmp" "$CRED_FILE"
  umask 022
  log "未通过平台 Secret 提供令牌，已随机生成并写入 $CRED_FILE"
  log "管理令牌(AdminToken): $RESIN_ADMIN_TOKEN"
  log "代理令牌(ProxyToken): $RESIN_PROXY_TOKEN"
fi
export RESIN_ADMIN_TOKEN RESIN_PROXY_TOKEN

# ---------- 状态库：活跃库放本地盘，启动时从持久盘快照恢复 ----------
if [ ! -s "$STATE_DIR/state.db" ] && [ -s "$SNAP_DB" ]; then
  if cp "$SNAP_DB" "$STATE_DIR/state.db"; then
    log "已从持久盘恢复 state.db（$(wc -c < "$STATE_DIR/state.db" 2>/dev/null || echo '?') 字节）"
  fi
fi

snapshot() {
  [ -s "$STATE_DIR/state.db" ] || return 0
  tmp="$DATA_DIR/.state.snap.$$"
  if ! sqlite3 "$STATE_DIR/state.db" ".backup '$tmp'" >/dev/null 2>&1; then
    rm -f "$tmp"
    return 0
  fi
  if [ "$(sqlite3 "$tmp" 'PRAGMA quick_check;' 2>/dev/null)" = "ok" ]; then
    [ -s "$SNAP_DB" ] && cp "$SNAP_DB" "$SNAP_PREV.tmp" 2>/dev/null && mv "$SNAP_PREV.tmp" "$SNAP_PREV" || true
    cp "$tmp" "$SNAP_DB.tmp" && mv "$SNAP_DB.tmp" "$SNAP_DB" && log "state.db 快照已更新（$(wc -c < "$SNAP_DB") 字节）"
  else
    log "警告：快照 quick_check 未通过，本次不覆盖持久盘"
  fi
  rm -f "$tmp"
}

# ---------- 启动 Resin（仅回环，公网走 nginx） ----------
export RESIN_STATE_DIR="$STATE_DIR" \
       RESIN_CACHE_DIR="$CACHE_DIR" \
       RESIN_LOG_DIR="$LOG_DIR" \
       RESIN_LISTEN_ADDRESS="${RESIN_LISTEN_ADDRESS:-127.0.0.1}" \
       RESIN_PORT="${RESIN_PORT:-2260}"

chown -R resin:resin "$STATE_DIR" "$CACHE_DIR" "$LOG_DIR" 2>/dev/null || true

log "启动 resin（监听 ${RESIN_LISTEN_ADDRESS}:${RESIN_PORT}，状态库 ${STATE_DIR}/state.db）"
su-exec resin:resin /usr/local/bin/resin &
RESIN_PID=$!

# 等 resin 就绪（最多 60s），再起 nginx
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
log "nginx 已启动，对外端口 7860 → 面板 /ui/ ，反代 /<令牌>/... ，额外端点 /e/1../e/5"

# 后台定时快照
(
  while sleep "$SNAP_INTERVAL"; do
    snapshot
  done
) &
SNAP_PID=$!

cleanup() {
  trap - TERM INT EXIT
  log "收到退出信号，收尾中"
  kill -QUIT "$NGX_PID" 2>/dev/null || true
  kill -TERM "$RESIN_PID" 2>/dev/null || true
  wait "$RESIN_PID" 2>/dev/null || true
  kill "$SNAP_PID" 2>/dev/null || true
  snapshot
  exit 0
}
trap cleanup TERM INT

while kill -0 "$RESIN_PID" 2>/dev/null && kill -0 "$NGX_PID" 2>/dev/null; do
  sleep 5
done
log "子进程退出，整体退出"
cleanup
