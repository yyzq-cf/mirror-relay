# 🔀 Mirror Relay

自托管 Docker 镜像中继缓存。部署在能访问 Docker Hub 等上游的服务器上，按需拉取并缓存镜像，让无法直接访问上游的服务器也能正常拉取。

## 工作原理

```
国内服务器                                中继服务器（部署 mirror-relay）
┌──────────┐                            ┌──────────────────────────┐
│ docker   │  pull relay-host/nginx     │  Nginx (:5000)           │
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

### 部署（中继服务器）

```bash
docker run -d \
  --name mirror-relay \
  --restart unless-stopped \
  -p 5000:5000 \
  -p 8080:8080 \
  -v mirror-data:/data \
  ywsj/mirror-relay:latest
```

启动后打开 `http://<你的服务器>:8080`，首次会生成随机密码，在服务器上执行以下命令获取：

```bash
docker exec mirror-relay cat /data/.initial_password
```

登录后请立即在「设置」页面修改密码。

### 使用（拉取端）

**方式一：Docker Hub 镜像加速（推荐）**

```json
// /etc/docker/daemon.json
{
  "registry-mirrors": ["http://<中继服务器IP>:5000"]
}
```

```bash
sudo systemctl restart docker
docker pull nginx:latest   # 自动通过中继拉取
```

**方式二：多上游显式拉取**

```bash
# Docker Hub 镜像
docker pull <中继地址>:5000/library/nginx:latest
docker pull <中继地址>:5000/username/repo:tag

# ghcr.io 镜像
docker pull <中继地址>:5000/ghcr/owner/repo:tag

# gcr.io 镜像
docker pull <中继地址>:5000/gcr/namespace/repo:tag
```

## 功能

| 功能 | 说明 |
|---|---|
| 多上游代理 | Docker Hub、ghcr.io、gcr.io，可在 Web 界面添加更多 |
| 按需缓存 | 只在被请求时拉取，不主动同步 |
| 自动清理 | 磁盘超阈值时 LRU 清理 + Registry GC |
| 白名单 | IP/CIDR 级别访问控制，Web 界面动态管理 |
| 管理面板 | 仪表盘、缓存列表、拉取日志、白名单管理、设置 |
| 手动 GC | 一键触发垃圾回收 |
| 缓存删除 | 支持删除指定缓存镜像 |

## 管理面板

| 页面 | 功能 |
|---|---|
| 仪表盘 | 磁盘使用、拉取统计、命中率、各上游缓存大小 |
| 缓存列表 | 查看所有缓存镜像，支持搜索和按上游筛选 |
| 拉取日志 | 最近 100 条拉取记录，命中/透传状态 |
| 白名单 | 开关白名单模式，增删 IP/CIDR，批量导入 |
| 设置 | 清理策略、上游管理、修改密码 |

## 白名单

- **关闭**（默认）：所有 IP 可拉取
- **开启**：只有白名单内 IP/CIDR 可拉取，其余返回 403
- 支持单 IP（`10.0.0.1`）和网段（`10.0.0.0/24`）
- 批量导入格式：每行一个 IP/CIDR，可用 `#` 添加备注
```
10.0.0.1 # 北京服务器
192.168.1.0/24 # 内网段
```

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
    restart: unless-stopped
    ports:
      - "5000:5000"
      - "8080:8080"
    volumes:
      - mirror-data:/data
    environment:
      - MAX_DISK_PCT=80
      - TARGET_DISK_PCT=70
      - CLEANUP_INTERVAL=300

volumes:
  mirror-data:
```

## 端口说明

| 端口 | 用途 |
|---|---|
| 5000 | 拉取端口（配置给客户端 docker daemon） |
| 8080 | 管理面板（浏览器访问） |

## 技术栈

- **Nginx**：路由层 + auth_request 鉴权
- **Docker Registry 2**：官方 registry proxy 模式
- **Flask**：Web UI + 鉴权 API
- **SQLite**：元数据存储
- **APScheduler**：定时清理任务

## 致谢

- [Docker Registry](https://github.com/distribution/distribution) — 官方 registry 的 proxy 模式是本项目的核心
- [Flask](https://flask.palletsprojects.com/) — Python Web 框架
- [Nginx](https://nginx.org/) — 反向代理与路由

## License

MIT
