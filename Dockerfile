# Saturn MouseHunter Crawler Service Dockerfile
FROM python:3.12-slim

# 设置工作目录
WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 复制项目文件
COPY . .

# 安装 uv
RUN pip install uv

# 安装依赖
RUN uv sync

# 暴露端口
EXPOSE 8006

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8006/health/status || exit 1

# 启动命令
CMD ["uv", "run", "python", "-m", "src.main"]