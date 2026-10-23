#!/usr/bin/env bash
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# SPDX-License-Identifier: MIT

# A 兼容专项 live runner（宿主机入口，自包含：容器内再执行自身）。
#
# 本地环境输入（官方 CI 不执行 live 套件；缺失时脚本 fail-fast 并给出提示）：
#   DRIVER_WHL          GaussDB 官方自研驱动 whl 路径（宿主机路径即可，脚本按同
#                       路径只读挂进容器）
#   LIVE_PASSWORD_JSON  实例凭证 JSON 路径（顶层含 "password" 字段；同上挂进容器）
#   LIVE_HOST           GaussDB 实例地址（分布式/集中式均可，探测行为差异见
#                       同目录 README.md）
#
# 用法（仓库根）：
#   DRIVER_WHL=/path/to/GaussDB_Kernel_*-py311-none-linux_x86_64.whl \
#   LIVE_PASSWORD_JSON=/path/to/instance-payload.json \
#   LIVE_HOST=<instance-host> \
#     bash server/mcp_server_gaussdb_data/tests/integration/run_live_acompat.sh
#
# 流程（详见 tests/integration/README.md）：
#   1) python:3.11-slim 容器内装官方自研驱动 whl + 项目（套路同 run_live.sh）
#   2) acompat_provision.py create：经 MCP execute_sql_dangerous（confirm 流程）
#      尝试 CREATE DATABASE _acompat_probe DBCOMPATIBILITY='A'——分布式可能拒绝，
#      真实结果原样打印后退出（这本身就是探测结论）
#   3) 建库成功：跑 tests/integration/test_live_acompat.py（-m live）
#   4) acompat_provision.py drop 清理探测库
set -eu

if [ -d /repo ]; then
  # ==================== 容器内段 ====================
  REPO=/repo
  SERVER_DIR="$REPO/server/mcp_server_gaussdb_data"
  # 官方 monorepo：core 用仓库内 libs 源码包（不再有本地 vendored wheel）
  CORE_DIR="$REPO/libs/huaweicloud_mcp_core"
  # 本地环境输入：官方驱动 whl / 实例地址 / 凭证 JSON 均由 env 提供（宿主机段
  # 已把文件按同路径挂进容器），缺失即 fail-fast，不内置任何默认实例信息。
  DRIVER_WHL="${DRIVER_WHL:?DRIVER_WHL must point at the GaussDB official driver wheel (Linux/py3.11).}"
  : "${LIVE_HOST:?LIVE_HOST must be set to the GaussDB instance address.}"
  : "${LIVE_PASSWORD_JSON:?LIVE_PASSWORD_JSON must point at a JSON file with a top-level password field.}"
  TUNA=https://pypi.tuna.tsinghua.edu.cn/simple
  CRED=/tmp/live-credentials.json
  ACRED=/tmp/live-credentials-acompat.json

  # --- 1) 系统依赖 + Python 依赖（同 run_live.sh：addopts 带 --cov，插件必装 +
  #        pytest 追加 --no-cov，容器轨 site-packages 安装下覆盖率无意义） ---
  sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends libkeyutils1 libsasl2-2 libldap-common >/dev/null
  pip install --no-cache-dir -q -i "$TUNA" "mcp==2.3.0" "sqlparse==0.6.0" "pydantic==2.14.0"
  pip install --no-cache-dir -q -i "$TUNA" "$CORE_DIR"
  pip install --no-cache-dir -q --no-index "$DRIVER_WHL"
  pip install --no-cache-dir -q -i "$TUNA" "pytest==9.1.1" "pytest-asyncio==1.4.0" "pytest-cov==7.1.0"
  pip install --no-cache-dir -q -i "$TUNA" "$SERVER_DIR"

  SITE=$(python -c 'import site; print(site.getsitepackages()[0])')
  export LD_LIBRARY_PATH="$SITE/gaussdb_psycopg2.lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

  # --- 2) 主凭证（postgres 库）：实例由 LIVE_HOST / LIVE_PASSWORD_JSON 指定 ---
  python -c '
import json, os
with open(os.environ["LIVE_PASSWORD_JSON"], encoding="utf-8") as f:
    password = json.load(f)["password"]
creds = {
    "host": os.environ["LIVE_HOST"],
    "port": 8000,
    "dbname": "postgres",
    "user": "root",
    "password": password,
    "sslmode": "require",
}
with open("/tmp/live-credentials.json", "w", encoding="utf-8") as f:
    json.dump(creds, f)
os.chmod("/tmp/live-credentials.json", 0o600)
'

  # --- 3) 建库探测（经 MCP confirm 流程）；全部值被拒则原样打印并退出 ---
  # 分布式（GY1 实测）内部兼容模式用全称（pg_database 显示 ORA/MYSQL），先试 ORA；
  # 集中式（本地实测）接受短名 'A'。
  cd "$SERVER_DIR"
  export PYTHONDONTWRITEBYTECODE=1
  GAUSSDB_DATA_MCP_CREDENTIALS_FILE="$CRED" \
    python tests/integration/acompat_provision.py create --compat ORA,A,PG,B,C \
      --out "$ACRED"

  # --- 4) A 库专项测试（凭证指向 _acompat_probe） ---
  GAUSSDB_DATA_MCP_ACOMPAT_CREDENTIALS_FILE="$ACRED" \
    python -m pytest tests/integration/test_live_acompat.py -m live -v -p no:cacheprovider --no-cov

  # --- 5) 清理探测库 ---
  GAUSSDB_DATA_MCP_CREDENTIALS_FILE="$CRED" \
    python tests/integration/acompat_provision.py drop

  echo "ACOMPAT LIVE SUITE OK"
else
  # ==================== 宿主机段：投递容器后由容器内分支接管 ====================
  REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
  export MSYS_NO_PATHCONV=1   # Git Bash：防止 -v 的容器内路径被改写
  # DRIVER_WHL / LIVE_PASSWORD_JSON 是本地环境输入：按原路径只读挂进容器，容器内
  # 段即可直接引用；未设置时不挂载，容器内段 fail-fast。
  MOUNTS=()
  if [ -n "${DRIVER_WHL:-}" ]; then MOUNTS+=( -v "$DRIVER_WHL:$DRIVER_WHL:ro" ); fi
  if [ -n "${LIVE_PASSWORD_JSON:-}" ]; then MOUNTS+=( -v "$LIVE_PASSWORD_JSON:$LIVE_PASSWORD_JSON:ro" ); fi
  exec docker run --rm \
    -v "$REPO_ROOT:/repo:ro" \
    "${MOUNTS[@]}" \
    -e DRIVER_WHL="${DRIVER_WHL:-}" \
    -e LIVE_HOST="${LIVE_HOST:?LIVE_HOST is required (GaussDB instance address).}" \
    -e LIVE_PASSWORD_JSON="${LIVE_PASSWORD_JSON:-}" \
    -w /repo/server/mcp_server_gaussdb_data \
    python:3.11-slim \
    bash /repo/server/mcp_server_gaussdb_data/tests/integration/run_live_acompat.sh
fi
