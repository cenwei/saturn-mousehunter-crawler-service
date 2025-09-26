#!/bin/bash
# Saturn MouseHunter 爬虫服务 K8s 一键部署脚本

set -e

NAMESPACE="saturn-mousehunter"
APP_LABEL="app=saturn-crawler"

# 颜色输出
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

echo "🚀 Saturn MouseHunter 爬虫服务 K8s 一键部署"
echo "=============================================="

# 检查 kubectl
if ! command -v kubectl &> /dev/null; then
    log_error "kubectl 未安装"
    exit 1
fi

# 检查集群连接
if ! kubectl cluster-info &> /dev/null; then
    log_error "无法连接到 K8s 集群"
    exit 1
fi

log_info "集群连接正常"

# 创建命名空间
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

# 部署应用
log_info "开始部署..."
kubectl apply -f k8s/

# 等待 Pod 就绪
log_info "等待 Pod 启动..."
kubectl wait --for=condition=ready pod -l "$APP_LABEL" -n "$NAMESPACE" --timeout=300s

# 验证部署
log_info "验证部署状态..."
kubectl get pods -n "$NAMESPACE" -l "$APP_LABEL"
kubectl get services -n "$NAMESPACE"

log_success "🎉 部署完成！"

echo ""
echo "快速命令:"
echo "  查看状态: kubectl get pods -n $NAMESPACE -l $APP_LABEL"
echo "  查看日志: kubectl logs -f deployment/saturn-crawler-critical -n $NAMESPACE"
echo "  删除部署: kubectl delete -f k8s/"