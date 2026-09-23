# 🔀 Mirror Relay

自托管 Docker 镜像中继缓存。部署在能访问 Docker Hub 等上游的服务器上，按需拉取并缓存镜像，让无法直接访问上游的服务器也能正常拉取。

## ⚠️ 重要：必须配置 HTTPS 反向代理

Docker daemon 对非 localhost 的 registry **强制要求 HTTPS**。直接使用 `http://IP:5000` 拉取会报错：

```
Error response from daemon: http: server gave HTTP response to HTTPS client
```

因此**必须**通过 Nginx 反向代理配置 HTTPS 才能正常使用。你需要：

1. 一个域名（如 `mr.example.com`）解析到中继服务器
2. 申请 SSL 证书（Let's Encrypt 免费证书即可）
3. 配置 Nginx 反向代理，将 HTTPS 请求转发到容器的 5000 端口

Nginx 配置示例：

```nginx
server {
    listen 443 ssl;
    server_name mr.example.com;

    ssl_certificate /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;

    # Docker registry 不兼容 gzip
    gzip off;

    # 大镜像层传输超时放宽
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
    client_max_body_size 0;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

server {
    listen 80;
    server_name mr.example.com;
    return 301 https://$host$request_uri;
}
```

配置完成后，拉取端使用 `https://mr.example.com` 作为镜像地址。

> 如果仅在本机使用（`127.0.0.1`），可以直接用 HTTP，无需 HTTPS。

## 工作原理

```
国内服务器                                中继服务器（部署 mirror-relay）
┌──────────┐                            ┌──────────────────────────┐
│ docker   │  pull mr.example.com/nginx │  Nginx (:5000)           │
│ daemon   │ ──────────────────────────→│    ├── /  → Docker Hub   │
│          │                            │    ├── /ghcr/ → ghcr.io  │
│          │ ←──────────────────────────│    └── /gcr/  → gcr.io   │
└──────────┘                            │                          │
                                        │  Web UI (:8080)          │
                                        │  Cache Manager (sidecar) │
                                        └──────────────────────────┘
```

- **按需拉取**：没人拉取时中继服务器完全空闲，不主动同步任何镜像
- **首次透传**：收到请求且本地无缓存时，实时去上游拉取，拉完缓存一份
- **后续命中**：再次拉取同一镜像时直接从本地缓存返回
- **自动清理**：磁盘超阈值时按 LRU 删除最旧缓存，触发 GC 回收空间

## 快速开始

### 1. 部署（中继服务器）

```bash
docker run -d \
  --name mirror-relay \
  --restart always \
  -p 5000:5000 \
  -p 8080:8080 \
  -v ./data:/data \
  ywsj/mirror-relay:latest
```

启动后打开 `http://<你的服务器>:8080`，首次会生成随机密码，在服务器上执行以下命令获取：

```bash
docker exec mirror-relay cat /data/.initial_password
```

登录后请立即在「设置」页面修改密码。

### 2. 配置 Nginx 反向代理（必须）

按照上方「重要：必须配置 HTTPS 反向代理」章节配置 Nginx + SSL 证书。

### 3. 使用（拉取端）

**方式一：Docker Hub 镜像加速（推荐）**

```json
// /etc/docker/daemon.json
{
  "registry-mirrors": ["https://mr.example.com"]
}
```

```bash
sudo systemctl restart docker
docker pull nginx:latest   # 自动通过中继拉取
```

**方式二：多上游显式拉取**

```bash
# Docker Hub 镜像
docker pull mr.example.com/library/nginx:latest
docker pull mr.example.com/username/repo:tag

# ghcr.io 镜像
docker pull mr.example.com/ghcr/owner/repo:tag

# gcr.io 镜像
docker pull mr.example.com/gcr/namespace/repo:tag
```

## 功能

| 功能 | 说明 |
|---|---|
| 多上游代理 | Docker Hub、ghcr.io、gcr.io，可在 Web 界面添加更多 |
| 按需缓存 | 只在被请求时拉取，不主动同步 |
| 自动清理 | 磁盘超阈值时 LRU 清理 + Registry GC |
| 白名单 | IP/CIDR 级别访问控制，Web 界面动态管理 |
| 2FA 验证 | TOTP 两步验证，防止密码泄露 |
| 暴力破解防护 | 同一 IP 连续 5 次密码错误自动锁定 |
| 管理面板 | 仪表盘、缓存列表、拉取日志、白名单管理、设置 |
| 手动 GC | 一键触发垃圾回收 |
| 缓存删除 | 支持删除指定缓存镜像 |
| 明暗主题 | 支持深色/浅色主题切换 |

## 管理面板

| 页面 | 功能 |
|---|---|
| 仪表盘 | 服务状态、磁盘使用、拉取统计、命中率、各上游缓存大小 |
| 缓存列表 | 查看所有缓存镜像，支持搜索和按上游筛选 |
| 拉取日志 | 最近 100 条拉取记录，命中/透传/拒绝状态，来源 IP |
| 白名单 | 开关白名单模式，增删 IP/CIDR，批量导入 |
| 设置 | 清理策略、上游管理、2FA 管理、修改密码 |

## 白名单

- **关闭**（默认）：所有 IP 可拉取
- **开启**：只有白名单内 IP/CIDR 可拉取，其余返回 403
- 拒绝的请求会在拉取日志中显示原因（如「IP 不在白名单中」）
- 支持单 IP（`10.0.0.1`）和网段（`10.0.0.0/24`）
- 批量导入格式：每行一个 IP/CIDR，可用 `#` 添加备注

```
10.0.0.1 # 北京服务器
192.168.1.0/24 # 内网段
```

## 2FA 两步验证

1. 在「设置」页面点击「启用 2FA」
2. 用 Authenticator App（Google Authenticator、Authy 等）扫描二维码
3. 输入 6 位验证码确认
4. 之后每次登录需要密码 + 验证码双重验证
5. 可在设置页面关闭（需验证当前 2FA 码）

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `MAX_DISK_PCT` | 80 | 磁盘使用率阈值（%），超过触发清理 |
| `TARGET_DISK_PCT` | 70 | 清理后目标使用率（%） |
| `CLEANUP_INTERVAL` | 300 | 清理检查间隔（秒） |

以上配置也可在 Web 界面「设置」页面在线修改。

## Docker Compose

```yaml
services:
  mirror-relay:
    image: ywsj/mirror-relay:latest
    container_name: mirror-relay
    restart: always
    ports:
      - "5000:5000"
      - "8080:8080"
    volumes:
      - ./data:/data
    environment:
      - MAX_DISK_PCT=80
      - TARGET_DISK_PCT=70
      - CLEANUP_INTERVAL=300
```

数据保存在当前目录的 `data/` 目录下，方便备份和迁移。

## 端口说明

| 端口 | 用途 |
|---|---|
| 5000 | 拉取端口（通过 Nginx HTTPS 反代暴露） |
| 8080 | 管理面板（浏览器访问） |

## 技术栈

- **Nginx**：路由层 + auth_request 鉴权
- **Docker Registry 2**：官方 registry proxy 模式
- **Flask**：Web UI + 鉴权 API
- **SQLite**：元数据存储
- **APScheduler**：定时清理任务
- **pyotp**：2FA TOTP 验证

## 致谢

- [Docker Registry](https://github.com/distribution/distribution) — 官方 registry 的 proxy 模式是本项目的核心
- [Flask](https://flask.palletsprojects.com/) — Python Web 框架
- [Nginx](https://nginx.org/) — 反向代理与路由

## License

MIT
