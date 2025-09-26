#!/bin/bash
# Saturn MouseHunter 爬虫服务 K8s 自动化部署与验证脚本
# 确保部署成功的完整解决方案

set -e

# ========================================
# 配置参数
# ========================================
NAMESPACE="saturn-mousehunter"
APP_LABEL="app=saturn-crawler"
K8S_CONFIG_DIR="k8s"
TIMEOUT_SECONDS=300  # 5分钟超时
HEALTH_CHECK_RETRIES=10
HEALTH_CHECK_INTERVAL=30

# 部署组件
DEPLOYMENTS=("saturn-crawler-critical" "saturn-crawler-high" "saturn-crawler-normal")
EXPECTED_REPLICAS=(2 2 1)  # 对应每个部署的期望副本数
SERVICE_PORTS=(30006 30008 30009)  # 对应的 NodePort

# ========================================
# 颜色输出
# ========================================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
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

log_step() {
    echo -e "${PURPLE}[STEP]${NC} $1"
}

# ========================================
# 检查 kubectl 和集群连接
# ========================================
check_kubectl_connection() {
    log_step "检查 kubectl 配置和集群连接..."

    if ! command -v kubectl &> /dev/null; then
        log_error "kubectl 未安装或不在 PATH 中"
        exit 1
    fi

    if ! kubectl cluster-info &> /dev/null; then
        log_error "无法连接到 Kubernetes 集群，请检查 kubeconfig"
        exit 1
    fi

    CLUSTER_INFO=$(kubectl cluster-info 2>/dev/null | head -1)
    log_success "已连接到 K8s 集群"
    echo "  集群信息: $CLUSTER_INFO"

    # 检查节点状态
    NODE_COUNT=$(kubectl get nodes --no-headers 2>/dev/null | wc -l)
    READY_NODES=$(kubectl get nodes --no-headers 2>/dev/null | grep -c "Ready" || echo "0")

    log_info "集群节点状态: $READY_NODES/$NODE_COUNT 就绪"

    if [[ $READY_NODES -eq 0 ]]; then
        log_error "没有就绪的节点"
        exit 1
    fi
}

# ========================================
# 创建命名空间
# ========================================
create_namespace() {
    log_step "创建/检查命名空间: $NAMESPACE"

    if kubectl get namespace "$NAMESPACE" &> /dev/null; then
        log_info "命名空间 $NAMESPACE 已存在"
    else
        kubectl create namespace "$NAMESPACE"
        log_success "命名空间 $NAMESPACE 创建成功"
    fi
}

# ========================================
# 验证配置文件
# ========================================
validate_k8s_configs() {
    log_step "验证 K8s 配置文件..."

    if [[ ! -d "$K8S_CONFIG_DIR" ]]; then
        log_error "K8s 配置目录不存在: $K8S_CONFIG_DIR"
        exit 1
    fi

    required_files=(
        "service.yaml"
        "configmap.yaml"
        "crawler-deployment.yaml"
        "hpa.yaml"
    )

    for file in "${required_files[@]}"; do
        if [[ ! -f "$K8S_CONFIG_DIR/$file" ]]; then
            log_error "必需的配置文件不存在: $K8S_CONFIG_DIR/$file"
            exit 1
        fi
    done

    log_success "所有配置文件验证通过"

    # 验证 YAML 格式
    log_info "验证 YAML 格式..."
    for file in "${required_files[@]}"; do
        if ! kubectl apply --dry-run=client -f "$K8S_CONFIG_DIR/$file" &> /dev/null; then
            log_error "配置文件格式错误: $K8S_CONFIG_DIR/$file"
            exit 1
        fi
    done

    log_success "YAML 格式验证通过"
}

# ========================================
# 部署应用
# ========================================
deploy_application() {
    log_step "开始部署 Saturn MouseHunter 爬虫服务..."

    # 按顺序部署
    deployment_order=(
        "service.yaml"        # 首先创建服务
        "configmap.yaml"      # 然后创建配置
        "crawler-deployment.yaml"  # 部署应用
        "hpa.yaml"           # 最后启用自动扩缩容
    )

    for config_file in "${deployment_order[@]}"; do
        log_info "应用配置: $config_file"

        if kubectl apply -f "$K8S_CONFIG_DIR/$config_file"; then
            log_success "$config_file 应用成功"
        else
            log_error "$config_file 应用失败"
            exit 1
        fi

        # 给每个配置一些时间生效
        sleep 2
    done

    log_success "所有配置文件部署完成"
}

# ========================================
# 等待 Pod 就绪
# ========================================
wait_for_pods_ready() {
    log_step "等待 Pod 就绪..."

    local start_time=$(date +%s)
    local timeout_time=$((start_time + TIMEOUT_SECONDS))

    while true; do
        local current_time=$(date +%s)

        if [[ $current_time -gt $timeout_time ]]; then
            log_error "等待 Pod 就绪超时 (${TIMEOUT_SECONDS}s)"
            return 1
        fi

        # 检查所有 Pod 状态
        local total_pods=$(kubectl get pods -n "$NAMESPACE" -l "$APP_LABEL" --no-headers 2>/dev/null | wc -l)
        local ready_pods=$(kubectl get pods -n "$NAMESPACE" -l "$APP_LABEL" --no-headers 2>/dev/null | grep -c "1/1.*Running" || echo "0")

        log_info "Pod 状态: $ready_pods/$total_pods 就绪 (等待时间: $((current_time - start_time))s)"

        if [[ $total_pods -gt 0 && $ready_pods -eq $total_pods ]]; then
            log_success "所有 Pod 已就绪!"
            break
        fi

        # 显示未就绪的 Pod 详情
        local not_ready_pods=$(kubectl get pods -n "$NAMESPACE" -l "$APP_LABEL" --no-headers 2>/dev/null | grep -v "1/1.*Running" || true)
        if [[ -n "$not_ready_pods" ]]; then
            echo "未就绪的 Pod:"
            echo "$not_ready_pods" | while IFS= read -r line; do
                echo "  $line"
            done
        fi

        sleep 10
    done
}

# ========================================
# 验证 Deployment 状态
# ========================================
verify_deployments() {
    log_step "验证 Deployment 状态..."

    local all_deployments_ok=true

    for i in "${!DEPLOYMENTS[@]}"; do
        local deployment="${DEPLOYMENTS[i]}"
        local expected_replicas="${EXPECTED_REPLICAS[i]}"

        log_info "检查 Deployment: $deployment"

        # 检查 Deployment 是否存在
        if ! kubectl get deployment "$deployment" -n "$NAMESPACE" &> /dev/null; then
            log_error "Deployment $deployment 不存在"
            all_deployments_ok=false
            continue
        fi

        # 获取副本状态
        local ready_replicas=$(kubectl get deployment "$deployment" -n "$NAMESPACE" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")
        local desired_replicas=$(kubectl get deployment "$deployment" -n "$NAMESPACE" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "0")

        if [[ "$ready_replicas" == "$desired_replicas" && "$ready_replicas" -ge "$expected_replicas" ]]; then
            log_success "$deployment: $ready_replicas/$desired_replicas 副本就绪 ✅"
        else
            log_error "$deployment: $ready_replicas/$desired_replicas 副本就绪 (期望: $expected_replicas) ❌"
            all_deployments_ok=false
        fi
    done

    if [[ "$all_deployments_ok" == "true" ]]; then
        log_success "所有 Deployment 验证通过"
        return 0
    else
        log_error "部分 Deployment 验证失败"
        return 1
    fi
}

# ========================================
# 验证服务可访问性
# ========================================
verify_services() {
    log_step "验证服务可访问性..."

    # 获取集群节点 IP
    local node_ip=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
    if [[ -z "$node_ip" ]]; then
        node_ip=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="ExternalIP")].address}')
    fi

    if [[ -z "$node_ip" ]]; then
        log_warning "无法获取节点 IP，跳过外部访问验证"
        node_ip="localhost"
    fi

    log_info "使用节点 IP: $node_ip"

    local all_services_ok=true

    for i in "${!DEPLOYMENTS[@]}"; do
        local deployment="${DEPLOYMENTS[i]}"
        local port="${SERVICE_PORTS[i]}"
        local service_name=$(echo "$deployment" | sed 's/saturn-crawler/saturn-crawler-service/')

        log_info "测试服务: $service_name (端口: $port)"

        # 检查服务是否存在
        if kubectl get service "$service_name" -n "$NAMESPACE" &> /dev/null; then
            log_success "服务 $service_name 存在"

            # 尝试访问健康检查端点
            local health_url="http://$node_ip:$port/health/status"
            log_info "测试健康检查: $health_url"

            local retry_count=0
            local health_ok=false

            while [[ $retry_count -lt $HEALTH_CHECK_RETRIES ]]; do
                if curl -s -f --connect-timeout 5 "$health_url" > /dev/null 2>&1; then
                    local health_response=$(curl -s --connect-timeout 5 "$health_url" 2>/dev/null || echo "{}")
                    log_success "$service_name 健康检查通过 ✅"
                    echo "  响应: $health_response"
                    health_ok=true
                    break
                else
                    retry_count=$((retry_count + 1))
                    log_warning "$service_name 健康检查失败 (尝试 $retry_count/$HEALTH_CHECK_RETRIES)"

                    if [[ $retry_count -lt $HEALTH_CHECK_RETRIES ]]; then
                        sleep $HEALTH_CHECK_INTERVAL
                    fi
                fi
            done

            if [[ "$health_ok" == "false" ]]; then
                log_error "$service_name 健康检查持续失败 ❌"
                all_services_ok=false
            fi
        else
            log_error "服务 $service_name 不存在"
            all_services_ok=false
        fi
    done

    if [[ "$all_services_ok" == "true" ]]; then
        log_success "所有服务验证通过"
        return 0
    else
        log_error "部分服务验证失败"
        return 1
    fi
}

# ========================================
# 验证 HPA 状态
# ========================================
verify_hpa() {
    log_step "验证 HPA (自动扩缩容) 状态..."

    local hpa_count=$(kubectl get hpa -n "$NAMESPACE" --no-headers 2>/dev/null | wc -l)

    if [[ $hpa_count -eq 0 ]]; then
        log_warning "没有发现 HPA 配置"
        return 0
    fi

    log_info "发现 $hpa_count 个 HPA 配置"

    kubectl get hpa -n "$NAMESPACE" --no-headers | while IFS= read -r line; do
        local hpa_name=$(echo "$line" | awk '{print $1}')
        local targets=$(echo "$line" | awk '{print $3}')
        local min_replicas=$(echo "$line" | awk '{print $4}')
        local max_replicas=$(echo "$line" | awk '{print $5}')

        log_success "HPA $hpa_name: 目标=$targets, 范围=$min_replicas-$max_replicas ✅"
    done
}

# ========================================
# 生成部署报告
# ========================================
generate_deployment_report() {
    log_step "生成部署报告..."

    local report_file="/tmp/saturn-crawler-k8s-deployment-report.txt"

    cat > "$report_file" << EOF
# Saturn MouseHunter 爬虫服务 K8s 部署报告
生成时间: $(date)

## 集群信息
$(kubectl cluster-info 2>/dev/null)

## 命名空间
命名空间: $NAMESPACE

## Pod 状态
$(kubectl get pods -n "$NAMESPACE" -l "$APP_LABEL" -o wide 2>/dev/null)

## Deployment 状态
$(kubectl get deployments -n "$NAMESPACE" -o wide 2>/dev/null)

## Service 状态
$(kubectl get services -n "$NAMESPACE" -o wide 2>/dev/null)

## HPA 状态
$(kubectl get hpa -n "$NAMESPACE" 2>/dev/null || echo "无 HPA 配置")

## 事件日志
$(kubectl get events -n "$NAMESPACE" --sort-by=.metadata.creationTimestamp --field-selector type=Warning 2>/dev/null | tail -10)

## 访问地址
EOF

    # 添加访问地址
    local node_ip=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
    if [[ -n "$node_ip" ]]; then
        for i in "${!DEPLOYMENTS[@]}"; do
            local deployment="${DEPLOYMENTS[i]}"
            local port="${SERVICE_PORTS[i]}"
            echo "- $deployment: http://$node_ip:$port/health/status" >> "$report_file"
        done
    fi

    echo "" >> "$report_file"
    echo "## 部署验证结果" >> "$report_file"
    echo "✅ 所有组件部署成功" >> "$report_file"

    log_success "部署报告已生成: $report_file"
    echo ""
    echo "=== 部署报告摘要 ==="
    cat "$report_file"
}

# ========================================
# 故障诊断
# ========================================
diagnose_failures() {
    log_step "执行故障诊断..."

    echo ""
    echo "=== 故障诊断信息 ==="

    # 检查失败的 Pod
    local failed_pods=$(kubectl get pods -n "$NAMESPACE" -l "$APP_LABEL" --no-headers 2>/dev/null | grep -v "1/1.*Running" || true)

    if [[ -n "$failed_pods" ]]; then
        log_error "发现失败的 Pod:"
        echo "$failed_pods"

        # 获取失败 Pod 的详细信息
        echo "$failed_pods" | while IFS= read -r line; do
            local pod_name=$(echo "$line" | awk '{print $1}')
            echo ""
            echo "=== Pod $pod_name 诊断信息 ==="
            echo "--- Pod 描述 ---"
            kubectl describe pod "$pod_name" -n "$NAMESPACE" 2>/dev/null | tail -20
            echo "--- Pod 日志 ---"
            kubectl logs "$pod_name" -n "$NAMESPACE" --tail=10 2>/dev/null || echo "无日志可用"
        done
    fi

    # 检查事件
    echo ""
    echo "=== 最近的警告事件 ==="
    kubectl get events -n "$NAMESPACE" --sort-by=.metadata.creationTimestamp --field-selector type=Warning 2>/dev/null | tail -5

    # 检查资源使用情况
    echo ""
    echo "=== 节点资源使用情况 ==="
    kubectl top nodes 2>/dev/null || echo "无法获取节点资源信息 (metrics-server 可能未安装)"

    echo ""
    echo "=== Pod 资源使用情况 ==="
    kubectl top pods -n "$NAMESPACE" 2>/dev/null || echo "无法获取 Pod 资源信息 (metrics-server 可能未安装)"
}

# ========================================
# 清理函数
# ========================================
cleanup() {
    log_info "清理临时文件..."
    # 这里可以添加清理逻辑
}

# ========================================
# 主函数
# ========================================
main() {
    echo "================================================"
    echo "Saturn MouseHunter 爬虫服务 K8s 自动化部署"
    echo "================================================"
    echo ""

    # 注册清理函数
    trap cleanup EXIT

    # 执行部署流程
    check_kubectl_connection
    create_namespace
    validate_k8s_configs
    deploy_application
    wait_for_pods_ready

    # 验证部署结果
    local deployment_success=true

    if ! verify_deployments; then
        deployment_success=false
    fi

    if ! verify_services; then
        deployment_success=false
    fi

    verify_hpa  # HPA 验证失败不算严重错误

    # 根据部署结果执行不同操作
    if [[ "$deployment_success" == "true" ]]; then
        log_success "🎉 Saturn MouseHunter 爬虫服务部署成功！"
        generate_deployment_report

        echo ""
        echo "=== 快速访问命令 ==="
        echo "# 查看 Pod 状态"
        echo "kubectl get pods -n $NAMESPACE -l $APP_LABEL"
        echo ""
        echo "# 查看服务状态"
        echo "kubectl get services -n $NAMESPACE"
        echo ""
        echo "# 查看日志"
        echo "kubectl logs -f deployment/saturn-crawler-critical -n $NAMESPACE"
        echo ""

        exit 0
    else
        log_error "❌ Saturn MouseHunter 爬虫服务部署失败！"
        diagnose_failures

        echo ""
        echo "=== 故障恢复建议 ==="
        echo "1. 检查集群资源是否充足"
        echo "2. 验证镜像是否可以正常拉取"
        echo "3. 检查配置文件中的环境变量"
        echo "4. 查看详细的 Pod 日志和事件"
        echo ""
        echo "# 删除失败的部署重新开始"
        echo "kubectl delete -f $K8S_CONFIG_DIR/"
        echo ""

        exit 1
    fi
}

# 执行主函数
main "$@"