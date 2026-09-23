#!/bin/sh
set -e

DATA_DIR="${MIRROR_DATA_DIR:-/data}"
REGISTRY_CONFIG_DIR="$DATA_DIR/registry-config"

mkdir -p "$DATA_DIR/registry/hub" "$DATA_DIR/registry/ghcr" "$DATA_DIR/registry/gcr"
mkdir -p "$REGISTRY_CONFIG_DIR"

# ── 生成 registry 配置文件 ──

cat > "$REGISTRY_CONFIG_DIR/hub.yml" <<'EOF'
version: 0.1
log:
  level: warn
storage:
  filesystem:
    rootdirectory: /data/registry/hub
  delete:
    enabled: true
  cache:
    blobdescriptor: inmemory
http:
  addr: :5001
proxy:
  remoteurl: https://registry-1.docker.io
EOF

cat > "$REGISTRY_CONFIG_DIR/ghcr.yml" <<'EOF'
version: 0.1
log:
  level: warn
storage:
  filesystem:
    rootdirectory: /data/registry/ghcr
  delete:
    enabled: true
  cache:
    blobdescriptor: inmemory
http:
  addr: :5002
proxy:
  remoteurl: https://ghcr.io
EOF

cat > "$REGISTRY_CONFIG_DIR/gcr.yml" <<'EOF'
version: 0.1
log:
  level: warn
storage:
  filesystem:
    rootdirectory: /data/registry/gcr
  delete:
    enabled: true
  cache:
    blobdescriptor: inmemory
http:
  addr: :5003
proxy:
  remoteurl: https://gcr.io
EOF

# ── 启动 registry 实例 (后台) ──
docker-registry serve "$REGISTRY_CONFIG_DIR/hub.yml" &
docker-registry serve "$REGISTRY_CONFIG_DIR/ghcr.yml" &
docker-registry serve "$REGISTRY_CONFIG_DIR/gcr.yml" &

# ── 启动 nginx (后台) ──
nginx -g 'daemon off;' &

# ── 启动 cache-manager (后台) ──
python3 /app/src/cache_manager.py &

# ── 启动 Web UI (前台) ──
exec gunicorn -w 1 -b 0.0.0.0:8080 app:app --chdir /app/src
