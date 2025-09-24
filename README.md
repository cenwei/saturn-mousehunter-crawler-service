# Saturn MouseHunter Crawler Service

Saturn MouseHunter 分布式爬虫微服务 - 负责从Dragonfly任务队列消费爬取任务，执行Web数据抓取。

## 服务概述

本服务是Saturn MouseHunter系统中的下游爬虫微服务，主要功能包括：

- 🚀 **Dragonfly任务队列消费**: 监听并消费来自调度器的爬取任务
- 🔄 **Cookie和代理注入**: 基于任务类型自动注入代理和Cookie
- 🌐 **HTTP爬虫引擎**: 高性能异步HTTP数据抓取
- 📊 **任务状态上报**: 实时上报任务执行状态和超时重试
- 🔍 **监控和指标收集**: 爬虫性能指标和运行状态监控

## 架构设计

### 微服务架构
```
┌─────────────────────────────────────────┐
│        Saturn MouseHunter系统           │
├─────────────────────────────────────────┤
│ ┌─────────────┐  ┌─────────────────────┐│
│ │ 调度器服务   │  │ 认证服务             ││
│ │ (8000)      │  │ (8001)              ││
│ └─────────────┘  └─────────────────────┘│
│        │                                │
│        │ Dragonfly Queue                │
│        ▼                                │
│ ┌─────────────────────────────────────┐ │
│ │       爬虫服务 (8006)               │ │
│ │  ┌─────────────┐ ┌─────────────────┐│ │
│ │  │任务消费者    │ │代理池集成        ││ │
│ │  └─────────────┘ └─────────────────┘│ │
│ │  ┌─────────────┐ ┌─────────────────┐│ │
│ │  │爬虫引擎     │ │状态上报          ││ │
│ │  └─────────────┘ └─────────────────┘│ │
│ └─────────────────────────────────────┘ │
│                  │                      │
│                  ▼                      │
│ ┌─────────────────────────────────────┐ │
│ │       代理池服务 (8005)              │ │
│ └─────────────────────────────────────┘ │
└─────────────────────────────────────────┘
```

### 任务流程
1. **任务接收**: 从Dragonfly队列接收爬取任务
2. **资源注入**: 根据任务类型注入代理和Cookie
3. **数据抓取**: 执行HTTP请求并解析数据
4. **状态上报**: 实时上报任务执行状态
5. **结果存储**: 将抓取结果发送到目标系统

## 快速开始

### 环境要求
- Python 3.12+
- Redis (Dragonfly) 6.0+
- 代理池服务 (可选)

### 安装依赖
```bash
cd saturn-mousehunter-crawler-service
uv sync
```

### 配置环境
复制并编辑环境配置文件：
```bash
cp .env.example .env
# 编辑.env文件，设置必要的配置项
```

### 启动服务
```bash
# 开发环境
CRAWLER_SERVICE_PORT=8006 uv run python -m main

# 生产环境
uv run python -m main
```

## 配置说明

### 核心配置项

#### 服务配置
```env
CRAWLER_SERVICE_PORT=8006       # 服务端口
DEBUG=false                     # 调试模式
LOG_LEVEL=INFO                  # 日志级别
```

#### Dragonfly队列配置
```env
DRAGONFLY_HOST=192.168.8.188    # Dragonfly主机
DRAGONFLY_PORT=30010            # Dragonfly端口
DRAGONFLY_DB=0                  # 数据库索引
```

#### 工作器配置
```env
WORKER_ID=crawler-worker-01     # 工作器ID
MAX_CONCURRENT_TASKS=5          # 最大并发任务数
TASK_TIMEOUT_SECONDS=300        # 任务超时时间
```

#### 任务类型支持
```env
SUPPORTED_TASK_TYPES=1m_realtime,5m_realtime,15m_realtime,15m_backfill,1d_backfill
SUPPORTED_MARKETS=CN,US,HK
```

## API接口

### 健康检查
- `GET /health/status` - 服务状态检查
- `GET /health/ready` - 就绪状态检查

### 爬虫管理
- `GET /api/v1/crawler/stats` - 爬虫统计信息
- `GET /api/v1/crawler/tasks/active` - 活跃任务列表
- `POST /api/v1/crawler/tasks/{task_id}/cancel` - 取消任务

### 监控指标
- `GET /metrics` - Prometheus格式指标

## 开发指南

### 目录结构
```
src/
├── application/          # 应用服务层
│   ├── consumer/        # Dragonfly任务消费者
│   └── services/        # 业务服务
├── domain/              # 领域模型
├── infrastructure/      # 基础设施层
│   ├── crawler/        # 爬虫引擎
│   ├── proxy/          # 代理管理
│   └── settings/       # 配置管理
└── interfaces/          # 接口层
    └── api/            # REST API
```

### 添加新的任务处理器
```python
from application.consumer.dragonfly_task_consumer import CrawlerTaskConsumer

# 注册任务处理器
@crawler_consumer.register_task_handler("custom_task_type")
async def handle_custom_task(task: DragonflyTask) -> bool:
    # 实现自定义任务处理逻辑
    return True
```

### 任务类型配置
支持的任务类型与队列优先级映射：
- `1m_realtime` → CRITICAL优先级（指标型队列）
- `5m_realtime` → CRITICAL优先级（热点型队列）
- `15m_realtime` → HIGH优先级（全量型队列）
- `15m_backfill` → MEDIUM优先级（全量型队列）
- `1d_backfill` → LOW优先级（全量型队列）

## 监控和运维

### 日志管理
服务使用loguru进行结构化日志记录：
```python
from saturn_mousehunter_shared.log.logger import get_logger
log = get_logger(__name__)

log.info("任务执行成功", task_id=task_id, duration=duration)
```

### 性能指标
- 任务消费速率
- 任务执行成功率
- 平均任务执行时间
- 活跃连接数
- 代理可用性

### 故障排查
1. **任务消费异常**: 检查Dragonfly连接和队列配置
2. **代理连接失败**: 检查代理池服务可用性
3. **任务执行超时**: 调整`TASK_TIMEOUT_SECONDS`配置
4. **并发限制**: 调整`MAX_CONCURRENT_TASKS`配置

## 部署说明

### Docker部署
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install uv && uv sync
EXPOSE 8006
CMD ["uv", "run", "python", "-m", "main"]
```

### Kubernetes部署
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: crawler-service
spec:
  replicas: 3
  selector:
    matchLabels:
      app: crawler-service
  template:
    metadata:
      labels:
        app: crawler-service
    spec:
      containers:
      - name: crawler-service
        image: saturn-mousehunter-crawler-service:latest
        ports:
        - containerPort: 8006
        env:
        - name: CRAWLER_SERVICE_PORT
          value: "8006"
        - name: DRAGONFLY_HOST
          value: "dragonfly-service"
```

## 许可证

MIT License - 详见 [LICENSE](LICENSE) 文件

## 贡献指南

1. Fork 项目
2. 创建特性分支 (`git checkout -b feature/amazing-feature`)
3. 提交更改 (`git commit -m 'Add amazing feature'`)
4. 推送到分支 (`git push origin feature/amazing-feature`)
5. 打开 Pull Request

## 联系方式

- 项目地址: https://github.com/cenwei/saturn-mousehunter-crawler-service
- 问题反馈: https://github.com/cenwei/saturn-mousehunter-crawler-service/issues