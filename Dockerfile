# 应用镜像：把后端跑起来（数据库由 docker-compose 里的 mysql 服务提供）
FROM python:3.11-slim

# 日志不缓冲，方便 docker logs 实时看
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# 先装依赖，利用镜像层缓存：只要 requirements.txt 没变就不会重装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷代码
COPY . .

EXPOSE 8000

# 容器里必须监听 0.0.0.0，否则宿主机访问不到
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
