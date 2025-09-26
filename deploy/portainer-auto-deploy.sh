#!/bin/bash
# Saturn MouseHunter 爬虫服务自动化部署脚本
# 通过 Portainer API 自动部署多优先级爬虫集群

set -e

# ========================================
# 配置参数
# ========================================
PORTAINER_URL="${PORTAINER_URL:-http://192.168.8.168:9000}"
PORTAINER_USERNAME="${PORTAINER_USERNAME:-admin}"
PORTAINER_PASSWORD="${PORTAINER_PASSWORD:-admin123}"
ENDPOINT_ID="${ENDPOINT_ID:-2}"  # Docker 环境 ID

# 服务配置
STACK_NAME="saturn-crawler-cluster"
IMAGE_NAME="saturn-mousehunter-crawler:latest"
NETWORK_NAME="saturn-crawler-network"

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
NC='\033[0m' # No Color

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
# Portainer API 认证
# ========================================
authenticate_portainer() {
    log_info "正在认证 Portainer API..."

    AUTH_RESPONSE=$(curl -s -X POST \
        "${PORTAINER_URL}/api/auth" \
        -H "Content-Type: application/json" \
        -d "{\"username\":\"${PORTAINER_USERNAME}\",\"password\":\"${PORTAINER_PASSWORD}\"}")

    if [[ $? -ne 0 ]]; then
        log_error "无法连接到 Portainer"
        exit 1
    fi

    JWT_TOKEN=$(echo "$AUTH_RESPONSE" | jq -r '.jwt // empty')

    if [[ -z "$JWT_TOKEN" || "$JWT_TOKEN" == "null" ]]; then
        log_error "Portainer 认证失败"
        echo "响应: $AUTH_RESPONSE"
        exit 1
    fi

    log_success "Portainer 认证成功"
    echo "Authorization: Bearer $JWT_TOKEN" > /tmp/portainer_headers
}

# ========================================
# 构建和推送 Docker 镜像
# ========================================
build_and_push_image() {
    log_info "开始构建 Docker 镜像..."

    # 检查 Dockerfile 是否存在
    if [[ ! -f "Dockerfile" ]]; then
        log_error "Dockerfile 不存在！"
        exit 1
    fi

    # 构建镜像
    docker build -t "$IMAGE_NAME" . || {
        log_error "Docker 镜像构建失败"
        exit 1
    }

    log_success "Docker 镜像构建完成: $IMAGE_NAME"

    # 如果需要推送到私有仓库
    if [[ -n "$DOCKER_REGISTRY" ]]; then
        log_info "推送镜像到私有仓库: $DOCKER_REGISTRY"
        docker tag "$IMAGE_NAME" "$DOCKER_REGISTRY/$IMAGE_NAME"
        docker push "$DOCKER_REGISTRY/$IMAGE_NAME" || {
            log_warning "推送镜像失败，使用本地镜像"
        }
    fi
}

# ========================================
# 生成 Docker Compose 配置
# ========================================
generate_compose_config() {
    log_info "生成 Docker Compose 配置..."

    cat > /tmp/saturn-crawler-compose.yml << EOF
version: '3.8'

services:
  saturn-crawler-critical:
    image: ${IMAGE_NAME}
    container_name: saturn-crawler-critical
    hostname: crawler-critical-\${HOSTNAME}
    ports:
      - "8006:8006"
    environment:
      CRAWLER_SERVICE_PORT: 8006
      WORKER_ID: crawler-critical-\${HOSTNAME}
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
      ENABLE_K8S_SCALING: false
      LOG_LEVEL: INFO
      GRACEFUL_SHUTDOWN_TIMEOUT: 120
      TASK_TIMEOUT_SECONDS: 300
    volumes:
      - crawler_critical_logs:/app/logs
      - crawler_critical_data:/app/data
    networks:
      - ${NETWORK_NAME}
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

  saturn-crawler-high:
    image: ${IMAGE_NAME}
    container_name: saturn-crawler-high
    hostname: crawler-high-\${HOSTNAME}
    ports:
      - "8008:8006"
    environment:
      CRAWLER_SERVICE_PORT: 8006
      WORKER_ID: crawler-high-\${HOSTNAME}
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
      ENABLE_K8S_SCALING: false
      LOG_LEVEL: INFO
      GRACEFUL_SHUTDOWN_TIMEOUT: 120
      TASK_TIMEOUT_SECONDS: 300
    volumes:
      - crawler_high_logs:/app/logs
      - crawler_high_data:/app/data
    networks:
      - ${NETWORK_NAME}
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

  saturn-crawler-normal:
    image: ${IMAGE_NAME}
    container_name: saturn-crawler-normal
    hostname: crawler-normal-\${HOSTNAME}
    ports:
      - "8009:8006"
    environment:
      CRAWLER_SERVICE_PORT: 8006
      WORKER_ID: crawler-normal-\${HOSTNAME}
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
      ENABLE_K8S_SCALING: false
      LOG_LEVEL: INFO
      GRACEFUL_SHUTDOWN_TIMEOUT: 120
      TASK_TIMEOUT_SECONDS: 600
    volumes:
      - crawler_normal_logs:/app/logs
      - crawler_normal_data:/app/data
    networks:
      - ${NETWORK_NAME}
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

volumes:
  crawler_critical_logs:
  crawler_critical_data:
  crawler_high_logs:
  crawler_high_data:
  crawler_normal_logs:
  crawler_normal_data:

networks:
  ${NETWORK_NAME}:
    driver: bridge
    ipam:
      config:
        - subnet: 172.21.0.0/16
EOF

    log_success "Docker Compose 配置生成完成"
}

# ========================================
# 检查现有 Stack
# ========================================
check_existing_stack() {
    log_info "检查现有 Stack: $STACK_NAME"

    EXISTING_STACK=$(curl -s -H @/tmp/portainer_headers \
        "${PORTAINER_URL}/api/stacks" | \
        jq -r ".[] | select(.Name == \"$STACK_NAME\") | .Id // empty")

    if [[ -n "$EXISTING_STACK" ]]; then
        log_warning "发现现有 Stack (ID: $EXISTING_STACK)，将进行更新"
        return 0
    else
        log_info "没有发现现有 Stack，将创建新 Stack"
        return 1
    fi
}

# ========================================
# 创建新 Stack
# ========================================
create_stack() {
    log_info "创建新的 Saturn Crawler Stack..."

    COMPOSE_CONTENT=$(cat /tmp/saturn-crawler-compose.yml | base64 -w 0)

    CREATE_RESPONSE=$(curl -s -X POST \
        "${PORTAINER_URL}/api/stacks/create/standalone/string" \
        -H @/tmp/portainer_headers \
        -H "Content-Type: application/json" \
        -d "{
            \"name\": \"$STACK_NAME\",
            \"stackFileContent\": \"$(cat /tmp/saturn-crawler-compose.yml | sed 's/"/\\"/g' | tr '\n' ' ')\",
            \"env\": [
                {\"name\": \"HOSTNAME\", \"value\": \"$(hostname)\"}
            ],
            \"endpointId\": $ENDPOINT_ID
        }")

    STACK_ID=$(echo "$CREATE_RESPONSE" | jq -r '.Id // empty')

    if [[ -n "$STACK_ID" && "$STACK_ID" != "null" ]]; then
        log_success "Stack 创建成功 (ID: $STACK_ID)"
        return 0
    else
        log_error "Stack 创建失败"
        echo "响应: $CREATE_RESPONSE"
        return 1
    fi
}

# ========================================
# 更新现有 Stack
# ========================================
update_stack() {
    local stack_id=$1
    log_info "更新现有 Stack (ID: $stack_id)..."

    UPDATE_RESPONSE=$(curl -s -X PUT \
        "${PORTAINER_URL}/api/stacks/$stack_id" \
        -H @/tmp/portainer_headers \
        -H "Content-Type: application/json" \
        -d "{
            \"stackFileContent\": \"$(cat /tmp/saturn-crawler-compose.yml | sed 's/"/\\"/g' | tr '\n' ' ')\",
            \"env\": [
                {\"name\": \"HOSTNAME\", \"value\": \"$(hostname)\"}
            ],
            \"pullImage\": true,
            \"endpointId\": $ENDPOINT_ID
        }")

    if echo "$UPDATE_RESPONSE" | jq -e '.Id' > /dev/null 2>&1; then
        log_success "Stack 更新成功"
        return 0
    else
        log_error "Stack 更新失败"
        echo "响应: $UPDATE_RESPONSE"
        return 1
    fi
}

# ========================================
# 等待服务启动
# ========================================
wait_for_services() {
    log_info "等待爬虫服务启动..."

    services=("8006" "8008" "8009")
    service_names=("critical" "high" "normal")

    for i in "${!services[@]}"; do
        port="${services[i]}"
        name="${service_names[i]}"

        log_info "等待 ${name} 爬虫服务 (端口 ${port}) 启动..."

        max_attempts=30
        attempt=0

        while [[ $attempt -lt $max_attempts ]]; do
            if curl -s -f "http://localhost:${port}/health/status" > /dev/null 2>&1; then
                log_success "${name} 爬虫服务已启动"
                break
            fi

            attempt=$((attempt + 1))
            if [[ $attempt -eq $max_attempts ]]; then
                log_warning "${name} 爬虫服务启动超时，但继续部署..."
            else
                echo -n "."
                sleep 2
            fi
        done
    done
}

# ========================================
# 验证部署
# ========================================
verify_deployment() {
    log_info "验证部署状态..."

    STACK_INFO=$(curl -s -H @/tmp/portainer_headers \
        "${PORTAINER_URL}/api/stacks" | \
        jq -r ".[] | select(.Name == \"$STACK_NAME\")")

    if [[ -n "$STACK_INFO" ]]; then
        STACK_STATUS=$(echo "$STACK_INFO" | jq -r '.Status // "unknown"')
        log_success "Stack 状态: $STACK_STATUS"

        # 检查各个服务的健康状态
        services=("8006" "8008" "8009")
        service_names=("Critical" "High" "Normal")

        echo ""
        log_info "=== 爬虫服务健康检查 ==="
        for i in "${!services[@]}"; do
            port="${services[i]}"
            name="${service_names[i]}"

            if curl -s -f "http://localhost:${port}/health/status" > /dev/null 2>&1; then
                health_response=$(curl -s "http://localhost:${port}/health/status")
                log_success "${name} 优先级爬虫 (端口 ${port}): ✅ 健康"
                echo "  响应: $health_response"
            else
                log_error "${name} 优先级爬虫 (端口 ${port}): ❌ 不健康"
            fi
        done

        echo ""
        log_info "=== 部署信息 ==="
        echo "Stack 名称: $STACK_NAME"
        echo "镜像版本: $IMAGE_NAME"
        echo "Dragonfly 服务: ${DRAGONFLY_HOST}:${DRAGONFLY_PORT}"
        echo "代理池服务: ${PROXY_POOL_HOST}:${PROXY_POOL_PORT}"
        echo "Critical 爬虫: http://localhost:8006"
        echo "High 爬虫: http://localhost:8008"
        echo "Normal 爬虫: http://localhost:8009"

        return 0
    else
        log_error "无法获取 Stack 信息"
        return 1
    fi
}

# ========================================
# 清理临时文件
# ========================================
cleanup() {
    log_info "清理临时文件..."
    rm -f /tmp/portainer_headers /tmp/saturn-crawler-compose.yml
}

# ========================================
# 主函数
# ========================================
main() {
    echo "========================================"
    echo "Saturn MouseHunter 爬虫服务自动化部署"
    echo "========================================"
    echo ""

    # 检查依赖
    for cmd in curl jq docker; do
        if ! command -v "$cmd" &> /dev/null; then
            log_error "缺少依赖: $cmd"
            exit 1
        fi
    done

    # 执行部署流程
    authenticate_portainer
    build_and_push_image
    generate_compose_config

    if check_existing_stack; then
        EXISTING_STACK=$(curl -s -H @/tmp/portainer_headers \
            "${PORTAINER_URL}/api/stacks" | \
            jq -r ".[] | select(.Name == \"$STACK_NAME\") | .Id")
        update_stack "$EXISTING_STACK"
    else
        create_stack
    fi

    wait_for_services
    verify_deployment
    cleanup

    echo ""
    log_success "🎉 Saturn MouseHunter 爬虫集群部署完成！"
    echo ""
    echo "访问地址:"
    echo "  - Critical 优先级爬虫: http://localhost:8006"
    echo "  - High 优先级爬虫: http://localhost:8008"
    echo "  - Normal 优先级爬虫: http://localhost:8009"
    echo ""
    echo "Portainer 管理: ${PORTAINER_URL}"
}

# 执行主函数
main "$@"