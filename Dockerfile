# ============ 阶段 1: 前端构建 ============
FROM node:20-alpine AS frontend

WORKDIR /build

# 先复制依赖清单，利用 Docker 层缓存：依赖不变时不重复安装
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund

COPY frontend/ ./
# outDir 由 vite.config.ts 解析为 ../app/static/dist，
# 在构建层中即 /app/static/dist（见下方 COPY --from=frontend）
RUN npm run build


# ============ 阶段 2: Python 依赖 ============
FROM python:3.11-slim AS backend

RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ && \
    pip config set install.trusted-host mirrors.aliyun.com

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ============ 阶段 3: 运行镜像 ============
FROM python:3.11-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true

# 只装运行时依赖：MySQL 客户端工具 + 健康检查用的 curl + 降权工具 gosu
RUN apt-get update && apt-get install -y --no-install-recommends \
    default-mysql-client \
    curl \
    gosu \
    && rm -rf /var/lib/apt/lists/*

COPY --from=backend /install /usr/local

WORKDIR /app

COPY app/ ./app/
COPY run.py ./
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# 前端构建产物（node 层在最终镜像中被丢弃）
COPY --from=frontend /app/static/dist ./app/static/dist

RUN chmod +x /usr/local/bin/docker-entrypoint.sh && \
    mkdir -p /app/data /app/backups

# 创建非 root 用户并移交 /app 所有权。
#
# 注意：这里不写 USER dbsync —— 入口脚本需要以 root 启动，
# 以便修正挂载目录属主后再降权执行服务。
# 服务进程本身仍以 dbsync 身份运行（见 docker-entrypoint.sh）。
RUN useradd --create-home --shell /bin/bash dbsync && \
    chown -R dbsync:dbsync /app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "run.py"]
