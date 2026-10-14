FROM python:3.11-slim
# 国内网络构建：apt 走清华源
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update -qq && apt-get install -y -qq --no-install-recommends \
       libkeyutils1 libsasl2-2 libldap-common && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY wheels/ ./wheels/
COPY src/ ./src/
# HCE 2.0 宿主上的 docker build RUN 内 umask 为 0027（安全基线），
# pip 产物会带 640/750 权限，导致镜像内非 root 用户（mcp）无法 import——显式还原 022
RUN umask 022 && pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    wheels/*.whl .
# wheels/ 内除 core（入库）外，部署者须把官方自研驱动 whl（GaussDB_Kernel_*.whl，
# 从官方发布包按 OS 取对应版本）复制进来一并安装——不入库，见 README §6.3
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.11/site-packages/gaussdb_psycopg2.lib
RUN useradd -m mcp && chown -R mcp /app
USER mcp
EXPOSE 8000
CMD ["gaussdb-data-mcp", "-t", "streamable-http"]
