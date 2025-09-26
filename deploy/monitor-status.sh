#!/bin/bash
# Saturn MouseHunter 爬虫服务 K8s 部署状态监控脚本

NAMESPACE="saturn-mousehunter"
APP_LABEL="app=saturn-crawler"

# 颜色输出
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# 获取节点IP
get_node_ip() {
    kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null ||
    kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="ExternalIP")].address}' 2>/dev/null ||
    echo "localhost"
}

# 健康检查
check_health() {
    local service=$1
    local port=$2
    local node_ip=$(get_node_ip)

    if curl -s -f --connect-timeout 5 "http://$node_ip:$port/health/status" > /dev/null 2>&1; then
        local health_data=$(curl -s --connect-timeout 5 "http://$node_ip:$port/health/status" 2>/dev/null)
        log_success "$service (端口 $port): ✅ 健康"
        echo "    响应: $health_data"
    else
        log_error "$service (端口 $port): ❌ 不健康"
    fi
}

while true; do
    clear
    echo "================================================"
    echo "🔍 Saturn MouseHunter 爬虫服务实时状态监控"
    echo "时间: $(date)"
    echo "================================================"

    # 检查集群连接
    if ! kubectl cluster-info &> /dev/null; then
        log_error "K8s 集群连接失败"
        sleep 10
        continue
    fi

    # Pod 状态
    echo ""
    log_info "📦 Pod 状态"
    echo "$(kubectl get pods -n "$NAMESPACE" -l "$APP_LABEL" 2>/dev/null || echo '无Pod运行')"

    # Deployment 状态
    echo ""
    log_info "🚀 Deployment 状态"
    kubectl get deployments -n "$NAMESPACE" 2>/dev/null | grep -E "(NAME|saturn-crawler)" || echo "无Deployment"

    # Service 状态
    echo ""
    log_info "🌐 Service 状态"
    kubectl get services -n "$NAMESPACE" 2>/dev/null | grep -E "(NAME|saturn-crawler)" || echo "无Service"

    # HPA 状态
    echo ""
    log_info "📈 HPA 状态"
    kubectl get hpa -n "$NAMESPACE" 2>/dev/null || echo "无HPA配置"

    # 健康检查
    echo ""
    log_info "🏥 健康检查"
    check_health "Critical爬虫" "30006"
    check_health "High爬虫" "30008"
    check_health "Normal爬虫" "30009"

    # 资源使用
    echo ""
    log_info "💻 资源使用情况"
    kubectl top pods -n "$NAMESPACE" 2>/dev/null | grep -E "(NAME|saturn-crawler)" || echo "无法获取资源信息"

    # 最近事件
    echo ""
    log_info "📋 最近事件 (警告)"
    kubectl get events -n "$NAMESPACE" --sort-by=.metadata.creationTimestamp --field-selector type=Warning 2>/dev/null | tail -3 || echo "无警告事件"

    echo ""
    echo "按 Ctrl+C 退出监控"
    sleep 30
done