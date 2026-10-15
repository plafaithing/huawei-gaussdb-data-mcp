FROM python:3.11-slim
# 国内网络构建：apt 走清华源
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update -qq && apt-get install -y -qq --no-install-recommends \
       libkeyutils1 libsasl2-2 libldap-common && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY wheels/ ./wheels/
COPY src/ ./src/
# 多架构构建（docker buildx --platform linux/amd64,linux/arm64，见 README §2.3/§6.3）：
# RUN 内 uname -m 即目标平台架构——删掉另一架构的驱动 whl（两架构 whl 同在 wheels/
# 时防止 pip 报「not a supported wheel」），再校验目标架构 whl 在位，缺失 fail-fast
# （提示用 fetch-driver.sh 取包），比 pip 的 no matching distribution 报错可读。
# HCE 2.0 宿主上的 docker build RUN 内 umask 为 0027（安全基线），pip 产物会带
# 640/750 权限，导致镜像内非 root 用户（mcp）无法 import——显式还原 022
RUN umask 022 \
    && case "$(uname -m)" in \
         x86_64)  rm -f wheels/*-py311-none-linux_aarch64.whl ;; \
         aarch64) rm -f wheels/*-py311-none-linux_x86_64.whl ;; \
         *) echo "unsupported build arch: $(uname -m) (need linux/amd64 or linux/arm64)" >&2; exit 1 ;; \
       esac \
    && ls wheels/GaussDB_Kernel_*-py311-none-linux_*.whl >/dev/null 2>&1 || { \
         echo "ERROR: no GaussDB_Kernel driver whl for $(uname -m) in wheels/." >&2; \
         echo "       Run ./fetch-driver.sh with the official driver package (README §2.3), then rebuild." >&2; exit 1; \
       } \
    && pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple wheels/*.whl .
# wheels/ 内除 core（入库）外，官方驱动 whl 由 fetch-driver.sh 或手动放入（不入库，
# 两架构可并存），随上一条一并安装
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.11/site-packages/gaussdb_psycopg2.lib
RUN useradd -m mcp && chown -R mcp /app
USER mcp
EXPOSE 8000
CMD ["gaussdb-data-mcp", "-t", "streamable-http"]
