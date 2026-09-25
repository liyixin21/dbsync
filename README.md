# DBSync

基于 MySQL binlog 的实时数据库同步与全量备份工具，支持将备份自动上传到 OpenList。

## 功能

- **实时同步** —— 消费源库 binlog，把行级变更实时应用到目标库，支持 INSERT/UPDATE/DELETE 与 DDL 转发。
- **一键全量复制** —— 把源库全量数据复制到目标库。
- **定时全量备份** —— `mysqldump` 全量备份，可设间隔与保留份数，支持下载与一键恢复。
- **备份上传** —— 备份完成后自动上传到 OpenList 指定目录，支持手动重传。
- **可观测** —— 每张表的同步状态与定位键、失败事件队列（DLQ）、真实端到端延迟、任务健康度。
- **WebUI** —— 单页应用，支持深浅色（含跟随系统）与主题色。

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.11 · FastAPI · SQLAlchemy 2.0 · SQLite |
| 同步 | mysql-replication（binlog 行事件）· mysql-connector |
| 备份 | mysqldump / mysql 子进程（asyncio，不阻塞事件循环） |
| 调度 | APScheduler（单例调度器） |
| 上传 | httpx → OpenList API |
| 前端 | Vue 3 · Vite · TypeScript · Pinia |
| 部署 | Docker 多阶段构建 |

## 快速开始

### Docker（推荐）

```bash
# SECRET_KEY 必填，缺失或过短会导致启动被拒绝
export SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
docker compose up -d --build
```

访问 http://localhost:8000 ，默认账号 `admin` / `admin123`，**首次登录后立即修改密码**。

### 本地开发

```bash
# 后端
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export SECRET_KEY="dev-secret-key-at-least-16-chars"
export DEBUG=true
python run.py            # http://127.0.0.1:8000

# 前端（另开终端，开发服务器带 HMR）
cd frontend
npm install
npm run dev              # http://127.0.0.1:5173，/api 代理到 8000

# 生产构建（产物输出到 app/static/dist，由后端托管）
npm run build
```

前端使用 history 路由，后端已配置 SPA 回退，`/databases` 这类子路径可直接刷新。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SECRET_KEY` | 无 | **必填**。JWT 签名与凭据加密的根密钥 |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | 监听地址 |
| `DEBUG` | `false` | 开发模式。`true` 时允许使用弱 SECRET_KEY |
| `DATABASE_URL` | `sqlite:///./data/dbsync.db` | 控制面数据库 |
| `BACKUP_DIR` | `./backups` | 备份文件目录 |
| `BACKUP_TIMEOUT` | `3600` | 单个备份/恢复的子进程超时（秒） |
| `LOG_LEVEL` / `LOG_FILE` | `INFO` / `./data/dbsync.log` | 日志 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | 令牌有效期 |
| `LOGIN_MAX_ATTEMPTS` / `LOGIN_WINDOW_SECONDS` | `10` / `300` | 登录失败限流 |
| `CORS_ORIGINS` | 空 | 跨域白名单，留空则不启用 CORS |
| `SYNC_MAX_TASKS` | `8` | 并发同步任务上限 |
| `SYNC_TX_MAX_ROWS` | `10000` | 单事务缓冲上限 |
| `SYNC_CHECKPOINT_INTERVAL` | `2.0` | 位点落盘间隔（秒） |
| `DLQ_MAX_ROWS` | `100000` | 失败事件队列上限 |

> `SECRET_KEY` 变更会导致已保存的数据库密码与 OpenList 密码无法解密。服务启动时会自检并列出受影响的记录。

## 使用流程

1. **配置数据库** —— 「数据库管理」添加源库与目标库，密码以 Fernet 加密存储，可「测试连接」。
2. **创建同步任务** —— 「同步管理」选择源库与目标库。启动前会校验源库配置。
3. **创建备份计划** —— 「备份管理」设置间隔与保留份数，可立即执行、下载、恢复。
4. **配置上传（可选）** —— 「设置 → 备份上传（OpenList）」填服务地址与账号，测试连接通过后，在备份计划中勾选「备份后上传」。
5. **查看状态** —— 「概览」显示健康度与磁盘占用；「日志管理」有三类审计日志。

## 源库前置条件

实时同步要求源库满足以下配置，否则**任务会拒绝启动**并给出修复指引：

```sql
-- 必须为 ROW：statement 格式的 DML 无法通过行事件复制
SET GLOBAL binlog_format = 'ROW';
-- 必须为 FULL：MINIMAL 下 binlog 只含主键，UPDATE 会缺失 SET 字段
SET GLOBAL binlog_row_image = 'FULL';
```

以上需同时写入 `my.cnf` 以持久化。另需为同步账号授予 `REPLICATION SLAVE`、`REPLICATION CLIENT` 权限。

`binlog_row_metadata` 为 `MINIMAL` 时同步仍可工作（按列顺序还原列名），但建议设为 `FULL`。

## 同步可靠性设计

| 机制 | 作用 |
|---|---|
| **事务边界** | 以 binlog XID 事件为提交点，源库一个事务在目标库原子落地 |
| **幂等写入** | INSERT 带 `ON DUPLICATE KEY UPDATE`，崩溃后重放无副作用 |
| **主键定位** | UPDATE/DELETE 用主键构造 WHERE，避免精度差异导致匹配失败 |
| **位点持久化** | 每事务提交后落盘（时间节流），停机强制落盘；优先使用 GTID |
| **错误分级** | 连接类不推进位点并退避重连；数据类跳过后继续并记入 DLQ |
| **缺表恢复** | 目标库缺表时标记状态并等待 DDL，不永久停摆 |
| **健康度** | 区分 正常/降级/停滞，DLQ 与每表位点在前端可见 |

### 关于无主键表

无主键（且无唯一键）的表**可以同步**，但 UPDATE/DELETE 只能靠全列值匹配定位。
若表中存在完全相同的多行，删除其中一行时会连带删除目标库中的其它相同行。

界面会把这类表标为「无主键」并给出提示。建议为其添加主键：

```sql
ALTER TABLE db_name.table_name ADD COLUMN id BIGINT AUTO_INCREMENT PRIMARY KEY;
```

## 备份上传（OpenList）

在「设置 → 备份上传」中配置：

| 项 | 说明 |
|---|---|
| 服务地址 | 如 `http://192.168.1.10:5244`，不带结尾斜杠 |
| 用户名 / 密码 | OpenList 账号，密码加密存储 |
| 默认远程目录 | 如 `/dbsync/backups`，不存在时自动逐级创建 |
| 校验 SSL | 自签名证书的内网服务可关闭 |

单个备份计划可指定独立目录覆盖全局默认。

上传基于 OpenList 官方 API（[认证](https://openlist.apifox.cn/api-128101241)、[流式上传](https://openlist.apifox.cn/api-128101260)）：

```
POST /api/auth/login   {"username","password"} → data.token（默认 48 小时）
PUT  /api/fs/put       Header: Authorization, File-Path(URL 编码)
                       Body: application/octet-stream
```

token 在进程内缓存，401 时自动重新登录重试一次。

**上传失败不会影响备份本身** —— 本地文件已落盘并通过完整性校验，仅记录失败原因，界面可见，可手动重传。

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

覆盖 SQL 生成、错误分类、应用器事务语义、引擎消费循环、API 契约、安全边界与 OpenList 上传。测试无需真实 MySQL 与 OpenList。

## 项目结构

```
dbsync/
├── app/
│   ├── main.py              # 应用装配、异常处理、SPA 回退、生命周期
│   ├── api/                 # REST 路由（统一响应外壳）
│   ├── core/                # 配置、数据库、安全、错误、分页、迁移
│   ├── models/              # SQLAlchemy 模型
│   ├── services/
│   │   ├── mysql/           # 客户端工具、表结构解析
│   │   ├── sync/            # 同步引擎（sql_builder / applier / checkpoint / engine）
│   │   ├── backup/          # 全量备份引擎与调度
│   │   ├── openlist/        # OpenList 上传客户端与配置
│   │   └── audit.py         # 统一日志写入
│   └── static/dist/         # 前端构建产物（由后端托管，不入库）
├── frontend/                # Vue 3 + Vite 源码
├── tests/                   # pytest
└── Dockerfile               # 三阶段构建
```

## 许可

MIT
