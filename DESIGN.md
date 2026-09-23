# mirror-relay 技术方案

## 项目定位

自托管 Docker 镜像中继缓存。部署在能访问 Docker Hub 等上游的服务器上，国内服务器配置该地址后即可按需拉取镜像，中继服务器只在被请求时才去上游拉取并缓存。

## 架构总览

```
国内服务器                                    香港服务器（部署 mirror-relay）
┌──────────┐                                ┌─────────────────────────────────────┐
│ docker   │  docker pull hk.cc/nginx:tag   │  Nginx (路由层, :5000)              │
│ daemon   │ ─────────────────────────────→ │    ├── /docker/* → registry-hub     │
│          │                                │    ├── /ghcr/*   → registry-ghcr    │
│ mirror   │ ←───────────────────────────── │    └── /gcr/*    → registry-gcr     │
│ config   │                                │                                     │
└──────────┘                                │  registry-hub  (proxy → Docker Hub) │
                                            │  registry-ghcr (proxy → ghcr.io)    │
                                            │  registry-gcr   (proxy → gcr.io)    │
                                            │                                     │
                                            │  cache-manager (sidecar)            │
                                            │    ├── 磁盘监控 + LRU 清理           │
                                            │    ├── 缓存元数据记录                │
                                            │    └── 定时任务                     │
                                            │                                     │
                                            │  web-ui (admin 面板, :8080)         │
                                            │    ├── 仪表盘（磁盘/镜像数/命中率）    │
                                            │    ├── 缓存列表 + 手动清理           │
                                            │    └── 上游配置管理                 │
                                            │                                     │
                                            │  SQLite (/data/mirror.db)           │
                                            │  镜像缓存 (/data/registry)          │
                                            └─────────────────────────────────────┘
```

## 组件设计

### 1. Nginx 路由层（端口 5000）

统一入口，按路径前缀分发到不同 registry 代理实例：

| 路径前缀 | 路由到 | 上游 |
|---|---|---|
| `/docker/` | registry-hub:5001 | registry-1.docker.io |
| `/ghcr/` | registry-ghcr:5002 | ghcr.io |
| `/gcr/` | registry-gcr:5003 | gcr.io |

用户使用方式：
```bash
# Docker Hub 镜像
docker pull hk.yourserver.com/docker/library/nginx:latest
docker pull hk.yourserver.com/docker/username/repo:tag

# ghcr.io 镜像
docker pull hk.yourserver.com/ghcr/owner/repo:tag

# gcr.io 镜像
docker pull hk.yourserver.com/gcr/namespace/repo:tag
```

兼容 Docker Hub mirror 模式（daemon.json registry-mirrors）：
```json
{
  "registry-mirrors": ["https://hk.yourserver.com"]
}
```
mirror 模式下路径不带前缀，Nginx 默认路由到 registry-hub。

### 2. Docker Registry 代理实例

每个上游一个官方 registry 实例，运行在 proxy 模式：

```yaml
# registry-hub config
version: 0.1
log:
  level: info
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
```

- ghcr / gcr 同理，换 remoteurl 和端口
- 按需拉取：收到请求 → 检查本地缓存 → 没有则实时去上游拉 → 缓存并返回
- 支持并发去重：registry 内部已有 singleflight 机制，同一镜像层不会重复拉取
- 存储共享同一个 /data volume，按上游分子目录

### 3. Cache Manager（sidecar 守护进程）

**Python + APScheduler**，和 registry 容器共享 /data volume。

#### 核心功能

| 功能 | 说明 |
|---|---|
| 磁盘监控 | 每 5 分钟扫描 /data/registry 目录，统计各上游缓存大小 |
| LRU 清理 | 磁盘用量超过阈值（默认 80%）时，按最久未访问排序，删旧镜像层直到降到 70% |
| 元数据记录 | 记录每次拉取的镜像名、tag、大小、时间到 SQLite |
| 命中率统计 | 统计缓存命中 vs 透传拉取次数 |
| GC 触发 | 清理后调用 registry 的垃圾回收 API 回收磁盘空间 |

#### 清理策略

```
磁盘使用 > MAX_DISK_PCT (默认 80%)
    ↓
扫描 /data/registry/*/，按 atime 排序
    ↓
删除最旧的 blob 直到使用率 ≤ TARGET_DISK_PCT (默认 70%)
    ↓
触发 registry GC: registry garbage-collect config.yml --delete-untagged
    ↓
更新 SQLite 元数据（标记已删除）
```

#### SQLite 表结构

```sql
-- 缓存镜像记录
CREATE TABLE cached_images (
    id INTEGER PRIMARY KEY,
    upstream TEXT NOT NULL,       -- hub / ghcr / gcr
    image_name TEXT NOT NULL,     -- library/nginx
    tag TEXT,                     -- latest
    digest TEXT,                  -- sha256:xxx
    size_bytes INTEGER,
    first_pulled_at DATETIME,
    last_pulled_at DATETIME,
    pull_count INTEGER DEFAULT 1,
    UNIQUE(upstream, image_name, tag)
);

-- 拉取日志
CREATE TABLE pull_logs (
    id INTEGER PRIMARY KEY,
    upstream TEXT,
    image_name TEXT,
    tag TEXT,
    cache_hit BOOLEAN,            -- true=缓存命中, false=透传
    client_ip TEXT,
    pulled_at DATETIME
);

-- 配置
CREATE TABLE config (
    key TEXT PRIMARY KEY,
    value TEXT
);
```

### 4. Web UI（管理面板，端口 8080）

**Flask + 轻量前端**，读 SQLite 展示数据。

#### 页面

| 页面 | 功能 |
|---|---|
| 仪表盘 | 磁盘使用饼图、缓存镜像总数、总拉取次数、命中率、各上游缓存大小 |
| 缓存列表 | 表格：镜像名、tag、上游、大小、首次拉取时间、最后拉取时间、拉取次数；支持搜索和按上游筛选 |
| 拉取日志 | 最近 100 条拉取记录，显示命中/透传、镜像、来源IP、时间 |
| 上游管理 | 添加/删除/编辑上游 registry（如增加 quay.io） |
| 设置 | 磁盘阈值、清理周期、是否启用自动清理、TLS 配置 |
| 手动操作 | 手动触发 GC、删除指定镜像缓存、清空全部缓存 |

#### 技术选型

- 后端：Flask + SQLite（和你的其他项目栈一致）
- 前端：原生 HTML + CSS + 少量 JS（不引入 React/Vue，保持轻量）
- 鉴权：基本认证（admin 密码，类似你的其他项目风格）

### 5. Docker 打包

#### docker-compose.yml

```yaml
services:
  nginx:
    image: nginx:alpine
    ports:
      - "5000:5000"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
    depends_on:
      - registry-hub
      - registry-ghcr
      - registry-gcr
    restart: unless-stopped

  registry-hub:
    image: registry:2
    environment:
      - REGISTRY_PROXY_REMOTEURL=https://registry-1.docker.io
      - REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY=/data/registry/hub
    volumes:
      - registry-data:/data
    restart: unless-stopped

  registry-ghcr:
    image: registry:2
    environment:
      - REGISTRY_PROXY_REMOTEURL=https://ghcr.io
      - REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY=/data/registry/ghcr
    volumes:
      - registry-data:/data
    restart: unless-stopped

  registry-gcr:
    image: registry:2
    environment:
      - REGISTRY_PROXY_REMOTEURL=https://gcr.io
      - REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY=/data/registry/gcr
    volumes:
      - registry-data:/data
    restart: unless-stopped

  cache-manager:
    image: ywsj/mirror-relay:latest
    volumes:
      - registry-data:/data
    environment:
      - MAX_DISK_PCT=80
      - TARGET_DISK_PCT=70
      - CLEANUP_INTERVAL=300
    restart: unless-stopped

  web-ui:
    image: ywsj/mirror-relay:latest
    ports:
      - "8080:8080"
    volumes:
      - registry-data:/data
    restart: unless-stopped

volumes:
  registry-data:
```

#### Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 入口：环境变量 MODE 决定运行 cache-manager 还是 web-ui
CMD ["sh", "-c", "if [ \"$MODE\" = 'web' ]; then gunicorn -w 2 -b 0.0.0.0:8080 app:app; else python3 cache_manager.py; fi"]
```

## 目录结构

```
mirror-relay/
├── README.md
├── DESIGN.md                  ← 本文件
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── nginx/
│   └── nginx.conf             ← 路由配置
├── registry/
│   ├── hub.yml                ← Docker Hub proxy config
│   ├── ghcr.yml               ← ghcr.io proxy config
│   └── gcr.yml                ← gcr.io proxy config
├── src/
│   ├── app.py                 ← Flask web UI
│   ├── cache_manager.py       ← 缓存清理守护进程
│   ├── db.py                  ← SQLite 模型
│   └── templates/             ← 前端模板
│       ├── base.html
│       ├── dashboard.html
│       ├── cache_list.html
│       ├── logs.html
│       └── settings.html
└── scripts/
    └── gc.sh                  ← registry garbage collect 封装
```

## 用户使用流程

### 1. 部署端（香港服务器）

```bash
docker compose pull
docker compose up -d
# 打开 http://hk-ip:8080 设置管理员密码
```

### 2. 使用端（国内服务器）

方式一：Docker Hub 镜像加速（推荐）
```json
// /etc/docker/daemon.json
{
  "registry-mirrors": ["https://hk.yourserver.com"]
}
```
```bash
systemctl restart docker
docker pull nginx:latest   # 自动走香港中继
```

方式二：多上游显式拉取
```bash
docker pull hk.yourserver.com/docker/library/nginx:latest
docker pull hk.yourserver.com/ghcr/owner/image:tag
docker pull hk.yourserver.com/gcr/namespace/image:tag
```

## 配置项

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `MAX_DISK_PCT` | 80 | 磁盘使用率阈值，超过触发清理 |
| `TARGET_DISK_PCT` | 70 | 清理后目标使用率 |
| `CLEANUP_INTERVAL` | 300 | 清理检查间隔（秒） |
| `ADMIN_PASSWORD` | 随机生成 | Web UI 管理员密码 |
| `TLS_CERT` | 无 | HTTPS 证书路径（可选） |
| `TLS_KEY` | 无 | HTTPS 密钥路径（可选） |

## 访问控制（白名单模式）

### 设计

Nginx 使用 `auth_request` 模块，每个拉取请求先发子请求到 Flask 鉴权端点验证，通过才放行到 registry 代理。白名单状态和条目存在 SQLite，Web UI 动态管理，**不需要 reload nginx**。

### 工作流程

```
国内服务器 docker pull hk.cc/nginx
    ↓
Nginx 收到请求
    ↓
auth_request → Flask /api/auth/check
    ↓
白名单关闭？ ──是──→ 返回 200，放行
    │
    否
    ↓
请求 IP 在白名单？ ──是──→ 返回 200，放行
    │
    否
    ↓
返回 403，拒绝
```

### 白名单规则

- **关闭时**：所有请求放行，等同于公开缓存
- **开启时**：只有白名单内的 IP 能拉取，其余返回 403
- 白名单条目支持单 IP 和 CIDR 网段（如 `203.0.113.0/24`）
- Web UI 可随时增删条目，即时生效
- 管理面板（8080 端口）不受白名单限制，始终可访问（用管理员密码认证）

### SQLite 表结构（新增）

```sql
CREATE TABLE whitelist (
    id INTEGER PRIMARY KEY,
    ip_or_cidr TEXT NOT NULL UNIQUE,   -- 10.0.0.1 或 10.0.0.0/24
    label TEXT,                        -- 备注名，如 "北京服务器"
    created_at DATETIME
);

-- config 表增加开关
INSERT OR IGNORE INTO config(key, value) VALUES ('whitelist_enabled', '0');
-- '1' = 开启, '0' = 关闭
```

### Nginx 配置

```nginx
location / {
    auth_request /_auth;
    proxy_pass http://registry-upstream;
    # ...其他 proxy 头
}

location = /_auth {
    internal;
    proxy_pass http://web-ui:8080/api/auth/check;
    proxy_pass_request_body off;
    proxy_set_header Content-Length "";
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Original-URI $request_uri;
}
```

### Flask 鉴权端点

```python
@app.route('/api/auth/check', methods=['GET', 'POST'])
def auth_check():
    # 管理端口直接放行已在 nginx 层隔离，这里只管拉取鉴权
    enabled = db.get_config('whitelist_enabled') == '1'
    if not enabled:
        return '', 200  # 白名单关闭，放行

    client_ip = request.headers.get('X-Real-IP', '')
    if ip_in_whitelist(client_ip):
        return '', 200
    return '', 403
```

### Web UI 白名单管理页

| 元素 | 说明 |
|---|---|
| 开关按钮 | 开启/关闭白名单模式 |
| 添加条目 | 输入 IP 或 CIDR + 备注名，点击添加 |
| 条目列表 | 表格：IP/CIDR、备注、添加时间、删除按钮 |
| 批量导入 | 支持文本框批量粘贴 IP（每行一个） |

## 安全考虑

- 拉取访问控制：可选 IP 白名单（CIDR 级别），Web UI 动态管理
- Web UI 需要密码认证
- 建议通过反向代理加 HTTPS（用户已有实践）
- 管理面板（8080）与拉取端口（5000）分离，管理面板不受白名单限制

## 约束与限制

- 单实例部署，不支持集群
- 清理依赖文件系统 atime（需要挂载时加 relatime）
- Registry GC 期间短暂不可用（通常几秒）
- 不支持私有镜像拉取（后续可加 auth 透传）

## 版本规划

| 版本 | 目标 |
|---|---|
| v0.1.0 | 基础功能：3 上游代理 + Nginx 路由 + Docker 打包 |
| v0.2.0 | 管理面板：仪表盘 + 缓存列表 + 拉取日志 |
| v0.3.0 | 自动清理：LRU 策略 + GC 触发 + 磁盘告警 |
| v0.4.0 | 上游管理：Web 界面动态增删上游 |
| v0.5.0 | 进阶：私有镜像 auth 透传 + 多架构镜像支持 |
