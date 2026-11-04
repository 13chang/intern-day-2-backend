# 练习用镜像：按 uv.lock 安装依赖后由 uvicorn 启动服务
FROM python:3.12-slim

# 容器内默认访问不了 pypi.org / ghcr.io，构建时统一走镜像源
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

RUN pip install --no-cache-dir uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    APP_DB_PATH=/app/data/app.db \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 先只复制依赖清单，依赖层可以复用缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app
COPY scripts ./scripts

RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
