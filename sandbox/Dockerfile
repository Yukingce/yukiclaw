# 加固容器用的沙箱镜像（预装 pytest 等依赖）
FROM python:3.13-slim

# 预装测试/常用依赖，避免容器内断网现装
RUN pip install --no-cache-dir pytest pytest-asyncio

# useradd：Linux 中用于创建用户的命令; -m：创建用户的 home 目录，通常会创建：/home/AgentSandbox ; 
# -u 1000：指定用户的 UID（User ID）为 1000。
# AgentSandbox：新建用户的用户名。
RUN useradd -m -u 1000 AgentSandbox

# 工作目录 + Python 缓存目录都放在工作目录下（确保在可写 tmpfs 范围内）
ENV HOME=/home/AgentSandbox
ENV PYTHONPYCACHEPREFIX=/home/AgentSandbox/.pycache
# 给容器设置一个环境变量 PYTEST_ADDOPTS，它会被 pytest 自动读取，并把其中内容当成额外的命令行参数。
# -p no:cacheprovider 是禁用 pytest 的 cacheprovider 插件。cacheprovider 主要负责 pytest 的缓存功能,生成：.pytest_cache/
ENV PYTEST_ADDOPTS="-p no:cacheprovider"

WORKDIR /home/AgentSandbox
USER 1000:1000