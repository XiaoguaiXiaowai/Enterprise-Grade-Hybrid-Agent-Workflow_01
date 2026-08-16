FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
# 默认走阿里云 PyPI 镜像（阿里云服务器直连 PyPI 极慢）；可用 --build-arg PIP_INDEX_URL 覆盖
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt
COPY . .
ENV APP_DB_PATH=/app/data/app.db
RUN mkdir -p /app/data
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
