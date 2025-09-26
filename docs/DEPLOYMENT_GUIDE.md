# Saturn MouseHunter 爬虫服务自动化部署指南

## 🎯 部署方案概览

Saturn MouseHunter 爬虫服务提供了 4 种完全自动化的部署方案，适应不同的环境和需求：

### 方案对比表

| 方案 | 适用场景 | 优势 | 部署复杂度 |
|------|---------|------|-----------|
| **Portainer API** | 单机 Docker 环境 | 可视化管理、操作简单 | ⭐⭐ |
| **多宿主机** | 分布式物理机 | 高可用、负载分散 | ⭐⭐⭐ |
| **CI/CD 流水线** | 企业级开发 | 完全自动化、版本控制 | ⭐⭐⭐⭐ |
| **Kubernetes** | 云原生环境 | 动态伸缩、高可用 | ⭐⭐⭐⭐⭐ |

## 🚀 快速开始

### 方案一：Portainer API 自动部署

**适用场景**: 单机或小规模 Docker 环境

```bash
# 1. 进入爬虫服务目录
cd saturn-mousehunter-crawler-service

# 2. 配置环境变量 (可选)
export PORTAINER_URL="http://192.168.8.168:9000"
export PORTAINER_USERNAME="admin"
export PORTAINER_PASSWORD="admin123"

# 3. 执行自动部署
chmod +x deploy/portainer-auto-deploy.sh
./deploy/portainer-auto-deploy.sh
```

**部署结果**:
- Critical 优先级爬虫: http://localhost:8006
- High 优先级爬虫: http://localhost:8008
- Normal 优先级爬虫: http://localhost:8009

### 方案二：多宿主机自动部署

**适用场景**: 分布式物理机集群

```bash
# 1. 配置目标宿主机 (修改脚本中的 HOSTS 数组)
HOSTS=(
    "192.168.8.101:22:critical"   # 专用 Critical 优先级
    "192.168.8.102:22:high"       # 专用 High 优先级
    "192.168.8.103:22:normal"     # 专用 Normal 优先级
)

# 2. 配置 SSH 密钥
export SSH_USER="root"
export SSH_KEY="~/.ssh/id_rsa"

# 3. 执行分布式部署
chmod +x deploy/multi-host-deploy.sh
./deploy/multi-host-deploy.sh
```

**部署结果**:
- 192.168.8.101:8006 (Critical 爬虫)
- 192.168.8.102:8006 (High 爬虫)
- 192.168.8.103:8006 (Normal 爬虫)
- 自动生成 Nginx 负载均衡配置

### 方案三：CI/CD 自动化流水线

**适用场景**: 企业级持续集成环境

```bash
# 1. 配置 GitHub Secrets
# PORTAINER_USERNAME: Portainer 用户名
# PORTAINER_PASSWORD: Portainer 密码
# SSH_PRIVATE_KEY: SSH 私钥 (多宿主机部署需要)

# 2. 推送代码触发自动部署
git add .
git commit -m "feat: 更新爬虫服务"
git push origin main
```

**自动化流程**:
1. ✅ 代码测试和质量检查
2. 🐳 Docker 镜像构建和推送
3. 🚀 Portainer/多宿主机自动部署
4. 📊 健康检查和状态通知

### 方案四：Kubernetes 动态伸缩 (已实现)

使用之前创建的 K8s 配置文件:

```bash
# 部署到 K8s 集群
kubectl apply -f k8s/

# 查看部署状态
kubectl get pods -n saturn-mousehunter -l app=saturn-crawler
```

## ⚙️ 配置说明

### 核心环境变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `DRAGONFLY_HOST` | 192.168.8.188 | Dragonfly 队列服务地址 |
| `DRAGONFLY_PORT` | 30010 | Dragonfly 队列服务端口 |
| `PROXY_POOL_HOST` | 192.168.8.168 | 代理池服务地址 |
| `PROXY_POOL_PORT` | 8005 | 代理池服务端口 |
| `LOG_LEVEL` | INFO | 日志级别 |
| `GRACEFUL_SHUTDOWN_TIMEOUT` | 120 | 优雅关闭超时时间(秒) |

### 优先级配置

| 优先级 | 队列订阅 | 资源配置 | 并发任务数 |
|--------|---------|----------|-----------|
| **Critical** | `critical`, `high` | 2 CPU, 1G RAM | 10 |
| **High** | `high`, `normal` | 1.5 CPU, 768M RAM | 8 |
| **Normal** | `normal` | 1 CPU, 512M RAM | 5 |

## 🔧 高级配置

### 1. 自定义镜像仓库

```bash
# 使用私有镜像仓库
export DOCKER_REGISTRY="your-registry.com"
export IMAGE_NAME="your-registry.com/saturn-mousehunter-crawler:latest"
```

### 2. 负载均衡集成

生成的 Nginx 配置支持:
- ✅ 按优先级路由: `/critical/`, `/high/`, `/normal/`
- ✅ 健康检查和故障转移
- ✅ 集群状态聚合: `/health/cluster`

### 3. 监控集成

支持集成 Prometheus + Grafana:

```yaml
# 添加到 docker-compose.yml
services:
  saturn-crawler:
    # ... 其他配置
    labels:
      - "prometheus.io/scrape=true"
      - "prometheus.io/port=8006"
      - "prometheus.io/path=/metrics"
```

## 🛠️ 故障排查

### 常见问题

**1. Portainer 认证失败**
```bash
# 检查 Portainer 服务状态
curl -I http://192.168.8.168:9000
# 验证用户名密码
```

**2. SSH 连接失败**
```bash
# 测试 SSH 连接
ssh -i ~/.ssh/id_rsa root@192.168.8.101 "echo connected"
# 检查 SSH 密钥权限
chmod 600 ~/.ssh/id_rsa
```

**3. 服务启动失败**
```bash
# 查看容器日志
docker logs saturn-crawler-critical
# 检查健康状态
curl http://localhost:8006/health/status
```

### 日志位置

- **Portainer 部署**: Docker 容器日志
- **多宿主机部署**: `/opt/saturn-crawler/logs/`
- **K8s 部署**: `kubectl logs -n saturn-mousehunter pod-name`

## 📊 监控和维护

### 健康检查端点

```bash
# 服务状态
curl http://localhost:8006/health/status

# 详细信息
curl http://localhost:8006/health/detail

# 队列状态
curl http://localhost:8006/api/v1/crawler/queue-stats
```

### 扩缩容操作

**Portainer 环境**:
- 通过 Portainer UI 调整容器副本数

**多宿主机环境**:
- 修改宿主机列表重新运行部署脚本

**K8s 环境**:
```bash
# 手动扩容
kubectl scale deployment saturn-crawler-high --replicas=10 -n saturn-mousehunter

# 自动扩容 (已配置 HPA)
kubectl get hpa -n saturn-mousehunter
```

## 🚨 生产环境建议

1. **安全配置**:
   - 使用 TLS 加密通信
   - 配置防火墙和网络策略
   - 定期轮换 SSH 密钥和密码

2. **监控告警**:
   - 集成 Prometheus + AlertManager
   - 配置钉钉/企微告警通知
   - 设置关键指标阈值

3. **备份策略**:
   - 定期备份配置文件和日志
   - 数据库备份和恢复流程
   - 灾备环境准备

4. **性能优化**:
   - 根据实际负载调整资源配置
   - 监控队列积压和处理延迟
   - 定期清理日志和临时文件

---

**维护团队**: Saturn MouseHunter Team
**更新时间**: 2025-09-26
**版本**: v2.0