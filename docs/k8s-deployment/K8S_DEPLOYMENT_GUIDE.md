# Saturn MouseHunter 爬虫服务 K8s 部署指南

## 📋 概述

Saturn MouseHunter 爬虫服务采用 Kubernetes 部署架构，支持动态优先级调度和零停机扩缩容。

## 🏗️ 架构设计

### 多优先级部署策略

```
┌─────────────────────────────────────────────────────┐
│ 爬虫服务 K8s 集群                                    │
├─────────────────────────────────────────────────────┤
│ Critical Priority (2-8 replicas)                   │
│ ├── 队列: critical, high                            │
│ ├── 资源: 300m CPU, 512Mi Memory                   │
│ └── 超时: 300s, 并发: 10                           │
├─────────────────────────────────────────────────────┤
│ High Priority (2-10 replicas)                      │
│ ├── 队列: high, normal                             │
│ ├── 资源: 200m CPU, 256Mi Memory                   │
│ └── 超时: 300s, 并发: 8                            │
├─────────────────────────────────────────────────────┤
│ Normal Priority (1-5 replicas)                     │
│ ├── 队列: normal                                   │
│ ├── 资源: 100m CPU, 128Mi Memory                   │
│ └── 超时: 600s, 并发: 5                            │
└─────────────────────────────────────────────────────┘
```

### 队列订阅策略

- **Critical 部署**: 订阅 `critical` + `high` 队列，优先处理紧急任务
- **High 部署**: 订阅 `high` + `normal` 队列，平衡处理重要任务
- **Normal 部署**: 仅订阅 `normal` 队列，处理普通任务

## 🚀 部署步骤

### 1. 创建命名空间和基础配置

```bash
# 应用命名空间和服务配置
kubectl apply -f k8s/service.yaml

# 应用配置文件
kubectl apply -f k8s/configmap.yaml
```

### 2. 部署爬虫服务

```bash
# 部署多优先级爬虫服务
kubectl apply -f k8s/crawler-deployment.yaml
```

### 3. 启用自动扩缩容

```bash
# 启用HPA自动扩缩容
kubectl apply -f k8s/hpa.yaml
```

### 4. 验证部署状态

```bash
# 查看Pod状态
kubectl get pods -n saturn-mousehunter -l app=saturn-crawler

# 查看服务状态
kubectl get svc -n saturn-mousehunter

# 查看HPA状态
kubectl get hpa -n saturn-mousehunter
```

## 📊 监控和管理

### 查看实时状态

```bash
# 查看各优先级部署状态
kubectl get deployment -n saturn-mousehunter -l component=crawler-service

# 查看Pod资源使用情况
kubectl top pods -n saturn-mousehunter -l app=saturn-crawler

# 查看HPA扩缩容状态
kubectl describe hpa saturn-crawler-critical-hpa -n saturn-mousehunter
```

### 手动扩缩容

```bash
# 手动调整Critical优先级爬虫数量
kubectl scale deployment saturn-crawler-critical --replicas=6 -n saturn-mousehunter

# 手动调整High优先级爬虫数量
kubectl scale deployment saturn-crawler-high --replicas=8 -n saturn-mousehunter

# 手动调整Normal优先级爬虫数量
kubectl scale deployment saturn-crawler-normal --replicas=3 -n saturn-mousehunter
```

## 🔧 配置调优

### 根据业务场景调整

**交易时段配置** (盘中优先实时任务):
```bash
# 扩容实时任务处理能力
kubectl scale deployment saturn-crawler-critical --replicas=8 -n saturn-mousehunter
kubectl scale deployment saturn-crawler-high --replicas=10 -n saturn-mousehunter
kubectl scale deployment saturn-crawler-normal --replicas=2 -n saturn-mousehunter
```

**非交易时段配置** (优先回填任务):
```bash
# 扩容回填任务处理能力
kubectl scale deployment saturn-crawler-critical --replicas=4 -n saturn-mousehunter
kubectl scale deployment saturn-crawler-high --replicas=6 -n saturn-mousehunter
kubectl scale deployment saturn-crawler-normal --replicas=5 -n saturn-mousehunter
```

### 资源限制调整

修改 `k8s/crawler-deployment.yaml` 中的资源配置：

```yaml
resources:
  requests:
    cpu: 200m      # 根据实际负载调整
    memory: 256Mi
  limits:
    cpu: 500m      # 防止资源争抢
    memory: 512Mi
```

## 🛡️ 故障处理

### 健康检查失败

```bash
# 查看Pod详细状态
kubectl describe pod <pod-name> -n saturn-mousehunter

# 查看Pod日志
kubectl logs <pod-name> -n saturn-mousehunter --tail=100

# 查看爬虫服务健康状态
curl http://<node-ip>:30006/health/ready
curl http://<node-ip>:30006/health/live
```

### 优雅重启

```bash
# 滚动重启指定部署
kubectl rollout restart deployment saturn-crawler-high -n saturn-mousehunter

# 查看重启状态
kubectl rollout status deployment saturn-crawler-high -n saturn-mousehunter
```

### 紧急缩容

```bash
# 紧急情况下快速缩容
kubectl scale deployment saturn-crawler-normal --replicas=0 -n saturn-mousehunter
kubectl scale deployment saturn-crawler-high --replicas=1 -n saturn-mousehunter
```

## 📈 性能优化

### HPA调优建议

1. **扩容策略**: 根据队列深度和CPU使用率设置合理的扩容阈值
2. **缩容策略**: 设置较长的稳定期，避免频繁缩容影响任务执行
3. **资源指标**: 结合CPU、内存和自定义队列指标进行扩缩容决策

### 部署策略优化

1. **反亲和性**: 确保同优先级Pod分布在不同节点上
2. **资源配额**: 设置合理的资源请求和限制
3. **存储优化**: 使用本地存储提高I/O性能

## 🔒 安全配置

### RBAC权限

当前配置的权限包括：
- 读取Pod状态 (监控需要)
- 读取和更新Deployment (动态扩缩容需要)

### 网络策略

建议配置NetworkPolicy限制爬虫服务的网络访问：
- 允许访问Dragonfly队列服务
- 允许访问代理池服务
- 限制不必要的外部网络访问

## 📚 相关文档

- [爬虫任务规划系统文档](../saturn-mousehunter-market-data/docs/crawler_plans/)
- [Dragonfly队列集成指南](./dragonfly-integration.md)
- [优雅关闭机制文档](./graceful-shutdown.md)

---

**维护**: Saturn MouseHunter Team
**更新**: 2025-09-26