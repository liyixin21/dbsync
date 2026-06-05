# DBSync - 数据库实时同步备份工具

一个基于 MySQL binlog 的实时数据库同步与定时备份工具，提供 Material Design 3 风格的 WebUI 管理面板。

## 功能特性

- **实时同步**：基于 MySQL binlog，当主数据库有变化时实时同步到备用数据库
- **定时备份**：支持全量备份和增量备份，可设置备份间隔或 Cron 表达式
- **一键复制**：将源数据库所有数据完整复制到目标数据库
- **WebUI 管理面板**：Material Design 3 风格，支持主题色自定义和深色模式
- **Docker 部署**：一键打包为 Docker 镜像部署

## 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/your-username/dbsync.git
cd dbsync
```

### 2. 启动服务

```bash
# 构建并启动
docker-compose up -d

# 查看日志
docker-compose logs -f
```

### 3. 访问管理面板

打开浏览器访问 http://localhost:8000

## 环境变量配置

在 `docker-compose.yml` 中配置以下环境变量：

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8000` | 监听端口 |
| `DEBUG` | `false` | 调试模式 |
| `SECRET_KEY` | - | **必填**，用于 JWT 签名，请设置为随机长字符串 |
| `BACKUP_DIR` | `./backups` | 备份文件存储目录 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

**生产环境部署示例：**

```yaml
environment:
  - HOST=0.0.0.0
  - PORT=8000
  - DEBUG=false
  - SECRET_KEY=your-very-long-random-secret-key-here
  - LOG_LEVEL=WARNING
```

## 数据持久化

应用使用 Docker Volume 持久化以下数据：

- `dbsync-data`：SQLite 配置数据库和日志文件
- `dbsync-backups`：MySQL 备份文件

```bash
# 查看数据卷
docker volume ls | grep dbsync

# 备份数据
docker run --rm -v dbsync-data:/data -v $(pwd)/backup:/backup alpine tar czf /backup/dbsync-data.tar.gz -C /data .
```

## 使用说明

1. **添加数据库**：在「数据库管理」页面添加源数据库和目标数据库的连接信息
2. **创建同步任务**：在「同步管理」页面创建实时同步任务，支持启动/停止同步和一键复制
3. **创建备份计划**：在「备份管理」页面设置备份计划（全量/增量、间隔时间或 Cron 表达式）
4. **查看日志**：在「运行日志」页面查看同步和备份的运行记录
5. **主题设置**：在「设置」页面自定义主题色和深色模式

## 常用命令

```bash
# 启动服务
docker-compose up -d

# 停止服务
docker-compose down

# 重启服务
docker-compose restart

# 查看实时日志
docker-compose logs -f

# 重新构建并启动（代码更新后）
docker-compose up -d --build

# 进入容器调试
docker exec -it dbsync /bin/bash
```

## 技术栈

- **后端**：Python 3.11 + FastAPI
- **数据库**：SQLite（配置存储）+ MySQL（同步/备份目标）
- **前端**：Material Design 3 + 原生 JavaScript
- **部署**：Docker + Docker Compose

## 项目结构

```
dbsync/
├── app/
│   ├── main.py           # FastAPI 应用入口
│   ├── api/              # REST API 路由
│   ├── core/             # 核心配置和数据库
│   ├── models/           # 数据库模型
│   ├── services/         # 同步和备份服务
│   └── static/           # WebUI 静态文件
├── backups/              # 备份文件存储（Docker Volume）
├── data/                 # 应用数据（Docker Volume）
├── Dockerfile            # Docker 构建文件
├── docker-compose.yml    # Docker 编排配置
├── requirements.txt      # Python 依赖
└── run.py                # 启动脚本
```

## License

MIT
