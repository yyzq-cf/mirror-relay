FROM registry:2 AS registry-bin

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx \
    && rm -rf /var/lib/apt/lists/*

# 从官方 registry:2 镜像复制 registry 二进制
COPY --from=registry-bin /bin/registry /usr/local/bin/docker-registry

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY nginx/nginx.conf /etc/nginx/nginx.conf

RUN mkdir -p /data/registry/hub /data/registry/ghcr /data/registry/gcr \
    && mkdir -p /data/registry-config \
    && chmod +x /app/scripts/entrypoint.sh

EXPOSE 5000 8080

CMD ["/app/scripts/entrypoint.sh"]
