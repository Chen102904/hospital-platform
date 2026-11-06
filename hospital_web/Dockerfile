# 医院信息管理系统（Flask + MySQL）镜像
# 运行：docker compose up（见 docker-compose.yml）
FROM python:3.12-slim

# 工作目录
WORKDIR /app

# 先复制依赖清单（利用 Docker 层缓存：依赖不变就不重新装）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 端口（Flask 默认 5000）
EXPOSE 5000

# 生产用 gunicorn 多 worker，比 app.run 稳
# 如果没装 gunicorn，这里会自动回退？不会——所以 requirements 里已含，见下
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "app:app"]
