#!/bin/bash
# Saturn MouseHunter 爬虫服务指定宿主机自动化部署脚本
# 支持多机器部署和负载均衡

set -e

# ========================================
# 配置参数
# ========================================

# 目标宿主机列表 (IP:SSH_PORT:ROLE)
HOSTS=(
    "192.168.8.101:22:critical"
    "192.168.8.102:22:high"
    "192.168.8.103:22:normal"
    "192.168.8.104:22:high"
    "192.168.8.105:22:normal"
)

# SSH 配置
SSH_USER="${SSH_USER:-root}"
SSH_KEY="${SSH_KEY:-~/.ssh/id_rsa}"
SSH_TIMEOUT=30

# 部署配置
DEPLOY_DIR="/opt/saturn-crawler"
IMAGE_NAME="saturn-mousehunter-crawler:latest"
COMPOSE_PROJECT_NAME="saturn-crawler"

# 外部服务配置
DRAGONFLY_HOST="${DRAGONFLY_HOST:-192.168.8.188}"
DRAGONFLY_PORT="${DRAGONFLY_PORT:-30010}"
PROXY_POOL_HOST="${PROXY_POOL_HOST:-192.168.8.168}"
PROXY_POOL_PORT="${PROXY_POOL_PORT:-8005}"

# ========================================
# 颜色输出
# ========================================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# ========================================
# SSH 执行函数
# ========================================
execute_ssh() {
    local host=$1
    local command=$2
    local timeout=${3:-$SSH_TIMEOUT}

    ssh -i "$SSH_KEY" \
        -o ConnectTimeout=$timeout \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR \
        "$SSH_USER@$host" \
        "$command"
}

# ========================================
# 文件传输函数
# ========================================
transfer_file() {
    local host=$1
    local local_file=$2
    local remote_path=$3

    scp -i "$SSH_KEY" \
        -o ConnectTimeout=$SSH_TIMEOUT \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR \
        "$local_file" "$SSH_USER@$host:$remote_path"
}

# ========================================
# 检查宿主机连通性
# ========================================
check_host_connectivity() {
    local host_config=$1
    local host=$(echo "$host_config" | cut -d: -f1)
    local port=$(echo "$host_config" | cut -d: -f2)

    log_info "检查宿主机连通性: $host:$port"

    if execute_ssh "$host" "echo 'connected'" 2>/dev/null | grep -q "connected"; then
        log_success "$host 连接成功"
        return 0
    else
        log_error "$host 连接失败"
        return 1
    fi
}

# ========================================
# 安装 Docker 和依赖
# ========================================
install_dependencies() {
    local host=$1

    log_info "在 $host 上安装 Docker 和依赖..."

    execute_ssh "$host" "
        # 更新包管理器
        apt-get update -qq

        # 安装基础工具
        apt-get install -y curl jq htop

        # 检查 Docker 是否已安装
        if ! command -v docker &> /dev/null; then
            echo 'Docker 未安装，开始安装...'

            # 安装 Docker
            curl -fsSL https://get.docker.com -o get-docker.sh
            sh get-docker.sh
            rm get-docker.sh

            # 启动 Docker
            systemctl start docker
            systemctl enable docker

            echo 'Docker 安装完成'
        else
            echo 'Docker 已安装'
        fi

        # 检查 Docker Compose 是否已安装
        if ! command -v docker-compose &> /dev/null; then
            echo 'Docker Compose 未安装，开始安装...'

            # 安装 Docker Compose
            curl -L \"https://github.com/docker/compose/releases/latest/download/docker-compose-\$(uname -s)-\$(uname -m)\" -o /usr/local/bin/docker-compose
            chmod +x /usr/local/bin/docker-compose

            echo 'Docker Compose 安装完成'
        else
            echo 'Docker Compose 已安装'
        fi

        # 创建部署目录
        mkdir -p $DEPLOY_DIR
        cd $DEPLOY_DIR
    "

    if [[ $? -eq 0 ]]; then
        log_success "$host 依赖安装完成"
    else
        log_error "$host 依赖安装失败"
        return 1
    fi
}

# ========================================
# 生成 Docker Compose 配置
# ========================================
generate_compose_for_role() {
    local role=$1
    local host=$2

    case "$role" in
        "critical")
            cat > "/tmp/docker-compose-${host}.yml" << EOF
version: '3.8'

services:
  saturn-crawler-critical:
    image: ${IMAGE_NAME}
    container_name: saturn-crawler-critical
    hostname: crawler-critical-${host}
    ports:
      - "8006:8006"
    environment:
      CRAWLER_SERVICE_PORT: 8006
      WORKER_ID: crawler-critical-${host}
      PRIORITY_LEVEL: CRITICAL
      MAX_CONCURRENT_TASKS: 10
      DRAGONFLY_QUEUES: crawler_backfill_critical,crawler_realtime_critical
      QUEUE_PRIORITIES: CRITICAL,HIGH
      DRAGONFLY_HOST: ${DRAGONFLY_HOST}
      DRAGONFLY_PORT: ${DRAGONFLY_PORT}
      PROXY_POOL_HOST: ${PROXY_POOL_HOST}
      PROXY_POOL_PORT: ${PROXY_POOL_PORT}
      ENABLE_PROXY_INJECTION: true
      ENABLE_COOKIE_INJECTION: true
      LOG_LEVEL: INFO
      GRACEFUL_SHUTDOWN_TIMEOUT: 120
      TASK_TIMEOUT_SECONDS: 300
    volumes:
      - ./logs:/app/logs
      - ./data:/app/data
    restart: unless-stopped
    deploy:
      resources:
        limits:
          cpus: '2.0'
          memory: 1G
        reservations:
          cpus: '0.5'
          memory: 512M
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8006/health/status"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
EOF
            ;;
        "high")
            cat > "/tmp/docker-compose-${host}.yml" << EOF
version: '3.8'

services:
  saturn-crawler-high:
    image: ${IMAGE_NAME}
    container_name: saturn-crawler-high
    hostname: crawler-high-${host}
    ports:
      - "8006:8006"
    environment:
      CRAWLER_SERVICE_PORT: 8006
      WORKER_ID: crawler-high-${host}
      PRIORITY_LEVEL: HIGH
      MAX_CONCURRENT_TASKS: 8
      DRAGONFLY_QUEUES: crawler_backfill_high,crawler_realtime_high,crawler_backfill_normal
      QUEUE_PRIORITIES: HIGH,NORMAL
      DRAGONFLY_HOST: ${DRAGONFLY_HOST}
      DRAGONFLY_PORT: ${DRAGONFLY_PORT}
      PROXY_POOL_HOST: ${PROXY_POOL_HOST}
      PROXY_POOL_PORT: ${PROXY_POOL_PORT}
      ENABLE_PROXY_INJECTION: true
      ENABLE_COOKIE_INJECTION: true
      LOG_LEVEL: INFO
      GRACEFUL_SHUTDOWN_TIMEOUT: 120
      TASK_TIMEOUT_SECONDS: 300
    volumes:
      - ./logs:/app/logs
      - ./data:/app/data
    restart: unless-stopped
    deploy:
      resources:
        limits:
          cpus: '1.5'
          memory: 768M
        reservations:
          cpus: '0.3'
          memory: 384M
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8006/health/status"]
      interval: 30s
      timeout: 10s
      retries: 3
EOF
            ;;
        "normal")
            cat > "/tmp/docker-compose-${host}.yml" << EOF
version: '3.8'

services:
  saturn-crawler-normal:
    image: ${IMAGE_NAME}
    container_name: saturn-crawler-normal
    hostname: crawler-normal-${host}
    ports:
      - "8006:8006"
    environment:
      CRAWLER_SERVICE_PORT: 8006
      WORKER_ID: crawler-normal-${host}
      PRIORITY_LEVEL: NORMAL
      MAX_CONCURRENT_TASKS: 5
      DRAGONFLY_QUEUES: crawler_backfill_normal,crawler_realtime_normal
      QUEUE_PRIORITIES: NORMAL
      DRAGONFLY_HOST: ${DRAGONFLY_HOST}
      DRAGONFLY_PORT: ${DRAGONFLY_PORT}
      PROXY_POOL_HOST: ${PROXY_POOL_HOST}
      PROXY_POOL_PORT: ${PROXY_POOL_PORT}
      ENABLE_PROXY_INJECTION: true
      ENABLE_COOKIE_INJECTION: true
      LOG_LEVEL: INFO
      GRACEFUL_SHUTDOWN_TIMEOUT: 120
      TASK_TIMEOUT_SECONDS: 600
    volumes:
      - ./logs:/app/logs
      - ./data:/app/data
    restart: unless-stopped
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 512M
        reservations:
          cpus: '0.2'
          memory: 256M
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8006/health/status"]
      interval: 30s
      timeout: 10s
      retries: 3
EOF
            ;;
    esac
}

# ========================================
# 构建和分发 Docker 镜像
# ========================================
build_and_distribute_image() {
    log_info "构建 Docker 镜像..."

    # 本地构建镜像
    docker build -t "$IMAGE_NAME" . || {
        log_error "Docker 镜像构建失败"
        exit 1
    }

    # 保存镜像为 tar 文件
    docker save "$IMAGE_NAME" | gzip > "/tmp/${IMAGE_NAME//\//-}.tar.gz"

    log_success "镜像构建完成，开始分发到各个宿主机..."

    # 分发镜像到各个宿主机
    for host_config in "${HOSTS[@]}"; do
        host=$(echo "$host_config" | cut -d: -f1)
        role=$(echo "$host_config" | cut -d: -f3)

        log_info "分发镜像到 $host ($role)..."

        # 传输镜像文件
        transfer_file "$host" "/tmp/${IMAGE_NAME//\//-}.tar.gz" "$DEPLOY_DIR/image.tar.gz" || {
            log_error "$host 镜像传输失败"
            continue
        }

        # 加载镜像
        execute_ssh "$host" "
            cd $DEPLOY_DIR
            gunzip -f image.tar.gz
            docker load < image.tar
            rm -f image.tar
        " || {
            log_error "$host 镜像加载失败"
            continue
        }

        log_success "$host 镜像分发完成"
    done

    # 清理本地临时文件
    rm -f "/tmp/${IMAGE_NAME//\//-}.tar.gz"
}

# ========================================
# 部署到单个宿主机
# ========================================
deploy_to_host() {
    local host_config=$1
    local host=$(echo "$host_config" | cut -d: -f1)
    local port=$(echo "$host_config" | cut -d: -f2)
    local role=$(echo "$host_config" | cut -d: -f3)

    log_info "部署到 $host ($role 优先级)..."

    # 生成配置文件
    generate_compose_for_role "$role" "$host"

    # 传输配置文件
    transfer_file "$host" "/tmp/docker-compose-${host}.yml" "$DEPLOY_DIR/docker-compose.yml" || {
        log_error "$host 配置文件传输失败"
        return 1
    }

    # 停止现有服务
    execute_ssh "$host" "
        cd $DEPLOY_DIR
        docker-compose down --remove-orphans 2>/dev/null || true
    "

    # 启动新服务
    execute_ssh "$host" "
        cd $DEPLOY_DIR
        docker-compose up -d
    " || {
        log_error "$host 服务启动失败"
        return 1
    }

    log_success "$host 部署完成"

    # 清理临时文件
    rm -f "/tmp/docker-compose-${host}.yml"
}

# ========================================
# 验证部署
# ========================================
verify_deployment() {
    log_info "验证所有宿主机部署状态..."

    echo ""
    echo "=== 部署状态报告 ==="

    success_count=0
    total_count=${#HOSTS[@]}

    for host_config in "${HOSTS[@]}"; do
        host=$(echo "$host_config" | cut -d: -f1)
        role=$(echo "$host_config" | cut -d: -f3)

        # 检查服务健康状态
        if curl -s -f --connect-timeout 5 "http://$host:8006/health/status" > /dev/null 2>&1; then
            health_response=$(curl -s "http://$host:8006/health/status")
            log_success "$host ($role): ✅ 健康"
            echo "  响应: $health_response"
            ((success_count++))
        else
            log_error "$host ($role): ❌ 不健康"
        fi
    done

    echo ""
    echo "部署成功: $success_count/$total_count"

    if [[ $success_count -eq $total_count ]]; then
        log_success "🎉 所有宿主机部署成功！"
        return 0
    else
        log_warning "部分宿主机部署失败，请检查日志"
        return 1
    fi
}

# ========================================
# 生成 Nginx 负载均衡配置
# ========================================
generate_nginx_config() {
    log_info "生成 Nginx 负载均衡配置..."

    cat > "/tmp/saturn-crawler-nginx.conf" << EOF
# Saturn MouseHunter 爬虫服务负载均衡配置

upstream saturn_crawler_critical {
$(for host_config in "${HOSTS[@]}"; do
    host=$(echo "$host_config" | cut -d: -f1)
    role=$(echo "$host_config" | cut -d: -f3)
    if [[ "$role" == "critical" ]]; then
        echo "    server $host:8006 max_fails=3 fail_timeout=10s;"
    fi
done)
}

upstream saturn_crawler_high {
$(for host_config in "${HOSTS[@]}"; do
    host=$(echo "$host_config" | cut -d: -f1)
    role=$(echo "$host_config" | cut -d: -f3)
    if [[ "$role" == "high" ]]; then
        echo "    server $host:8006 max_fails=3 fail_timeout=10s;"
    fi
done)
}

upstream saturn_crawler_normal {
$(for host_config in "${HOSTS[@]}"; do
    host=$(echo "$host_config" | cut -d: -f1)
    role=$(echo "$host_config" | cut -d: -f3)
    if [[ "$role" == "normal" ]]; then
        echo "    server $host:8006 max_fails=3 fail_timeout=10s;"
    fi
done)
}

server {
    listen 8016;
    server_name _;

    location /critical/ {
        proxy_pass http://saturn_crawler_critical/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    }

    location /high/ {
        proxy_pass http://saturn_crawler_high/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    }

    location /normal/ {
        proxy_pass http://saturn_crawler_normal/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    }

    # 健康检查聚合端点
    location /health/cluster {
        access_log off;
        return 200 "OK";
        add_header Content-Type text/plain;
    }
}
EOF

    log_success "Nginx 配置生成完成: /tmp/saturn-crawler-nginx.conf"
    echo "请将配置文件部署到 Nginx 服务器并重新加载配置"
}

# ========================================
# 主函数
# ========================================
main() {
    echo "============================================="
    echo "Saturn MouseHunter 指定宿主机自动化部署"
    echo "============================================="
    echo ""

    # 检查依赖
    for cmd in ssh scp docker curl jq; do
        if ! command -v "$cmd" &> /dev/null; then
            log_error "缺少依赖: $cmd"
            exit 1
        fi
    done

    # 检查 SSH 密钥
    if [[ ! -f "$SSH_KEY" ]]; then
        log_error "SSH 密钥不存在: $SSH_KEY"
        exit 1
    fi

    log_info "目标宿主机列表:"
    for host_config in "${HOSTS[@]}"; do
        host=$(echo "$host_config" | cut -d: -f1)
        role=$(echo "$host_config" | cut -d: -f3)
        echo "  - $host ($role 优先级)"
    done
    echo ""

    # 检查所有宿主机连通性
    log_info "检查宿主机连通性..."
    failed_hosts=()
    for host_config in "${HOSTS[@]}"; do
        if ! check_host_connectivity "$host_config"; then
            failed_hosts+=("$host_config")
        fi
    done

    if [[ ${#failed_hosts[@]} -gt 0 ]]; then
        log_warning "以下宿主机连接失败，将跳过部署:"
        for host_config in "${failed_hosts[@]}"; do
            host=$(echo "$host_config" | cut -d: -f1)
            echo "  - $host"
        done
        echo ""
    fi

    # 过滤可用的宿主机
    available_hosts=()
    for host_config in "${HOSTS[@]}"; do
        host=$(echo "$host_config" | cut -d: -f1)
        if ! [[ " ${failed_hosts[*]} " =~ " ${host_config} " ]]; then
            available_hosts+=("$host_config")
        fi
    done

    if [[ ${#available_hosts[@]} -eq 0 ]]; then
        log_error "没有可用的宿主机"
        exit 1
    fi

    # 安装依赖
    log_info "安装依赖到各宿主机..."
    for host_config in "${available_hosts[@]}"; do
        host=$(echo "$host_config" | cut -d: -f1)
        install_dependencies "$host" &
    done
    wait

    # 构建和分发镜像
    build_and_distribute_image

    # 部署到各个宿主机
    log_info "开始并行部署..."
    for host_config in "${available_hosts[@]}"; do
        deploy_to_host "$host_config" &
    done
    wait

    # 等待服务启动
    log_info "等待服务启动..."
    sleep 10

    # 验证部署
    verify_deployment

    # 生成负载均衡配置
    generate_nginx_config

    echo ""
    log_success "🎉 Saturn MouseHunter 分布式爬虫集群部署完成！"
    echo ""
    echo "部署摘要:"
    for host_config in "${available_hosts[@]}"; do
        host=$(echo "$host_config" | cut -d: -f1)
        role=$(echo "$host_config" | cut -d: -f3)
        echo "  - $host:8006 ($role 优先级)"
    done
    echo ""
    echo "负载均衡配置: /tmp/saturn-crawler-nginx.conf"
}

# 执行主函数
main "$@"