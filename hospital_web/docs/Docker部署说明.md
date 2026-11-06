# Docker 一键部署说明

> 让医院信息系统用 `docker compose up` 一键跑起来，MySQL + Flask 两个容器自动编排。
> 前提：本机已装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)。

## 一条命令启动

```bash
cd hospital_web
docker compose up --build
```

首次启动会自动做三件事（`docker/init/` 里的 SQL 按文件名顺序执行）：

1. 建 15 张表（`01_create_tables.sql`）
2. 灌演示数据（`02_seed_data.sql`）
3. 起 Flask（gunicorn 多 worker）

启动完成后访问：

```
http://localhost:5000
```

登录账号：`admin` / `admin123`（其他角色 `gh01`/`ys01`/`yf01`/`sf01`，密码 `123456`）

## 停止 / 清理

```bash
docker compose down            # 停止并移除容器（保留数据卷）
docker compose down -v         # 连同数据卷一起删（下次启动重新灌数据）
```

## 目录结构

```
hospital_web/
├── Dockerfile              # Flask 应用镜像（python:3.12-slim + gunicorn）
├── docker-compose.yml      # 编排 mysql + web 两个服务
├── .dockerignore           # 排除 config_local.py（含密码）等
└── docker/
    └── init/               # MySQL 首次启动自动执行的 SQL
        ├── 01_create_tables.sql   # 建 15 张表
        └── 02_seed_data.sql       # 演示数据（admin/admin123）
```

## 关键设计点

1. **密码不硬编码进镜像**：`config_local.py` 被 `.dockerignore` 排除；Docker 里密码由
   `docker-compose.yml` 的 `DB_PASSWORD` 环境变量传给 Flask，MySQL 用 `MYSQL_ROOT_PASSWORD`。
2. **数据持久化**：MySQL 数据存在命名卷 `his_mysql_data`，`down` 不丢数据，`down -v` 才清。
3. **健康检查**：web 容器 `depends_on` mysql 的 `service_healthy`，等数据库就绪才起 Flask，
   避免「连不上数据库」的启动竞态。
4. **字符集**：MySQL 启动参数强制 `utf8mb4`，避免中文乱码。

## 为什么用 gunicorn

`python app.py` 里的 `app.run(debug=True)` 只适合本地开发。Docker 里用 gunicorn 起 2 个
worker，生产环境更稳。两者互不影响——本地开发照旧 `python app.py`。
