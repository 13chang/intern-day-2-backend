FROM mcr.microsoft.com/devcontainers/python:3.12-bookworm

# 开发容器只提供工具链（Git / Python / uv），业务服务由各自的 compose 启动
# 走国内镜像源，避免构建时访问 pypi.org 超时
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple uv
