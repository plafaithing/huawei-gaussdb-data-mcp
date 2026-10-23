#!/usr/bin/env bash
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# SPDX-License-Identifier: MIT

# Live 集成测试 runner（宿主机入口，自包含：容器内再执行自身）。
#
# 本地环境输入（官方 CI 不执行 live 套件；缺失时脚本 fail-fast 并给出提示）：
#   DRIVER_WHL          GaussDB 官方自研驱动 whl 路径（仅 Linux x86_64/ARM64 +
#                       py3.11；宿主机路径即可，脚本按同路径只读挂进容器）
#   LIVE_PASSWORD_JSON  实例凭证 JSON 路径（顶层含 "password" 字段；同上挂进容器）
#   LIVE_HOST           GaussDB 实例地址（IP 或域名）
#
# 用法（仓库根）：
#   DRIVER_WHL=/path/to/GaussDB_Kernel_*-py311-none-linux_x86_64.whl \
#   LIVE_PASSWORD_JSON=/path/to/instance-payload.json \
#   LIVE_HOST=<instance-host> \
#     bash server/mcp_server_gaussdb_data/tests/integration/run_live.sh
#
# 职责（详见同目录 README.md）：
#   1) 在 python:3.11-slim 容器内：apt/pip 装官方自研驱动 whl（Linux/py311 专用）
#      + 项目 + pytest
#   2) 从 LIVE_PASSWORD_JSON 读实例管理员密码，组装 0600 的
#      /tmp/live-credentials.json（密码不进命令行、不回显）
#   3) 跑 pytest tests/integration -m live（真库端到端，7 工具全覆盖）
#   4) streamable-http 冒烟：无 token 401 / 带 Bearer token initialize 200
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
  TOKENS=/tmp/live-tokens.json

  # --- 1) 系统依赖：自研驱动的间接链接依赖（keyutils/sasl/ldap）+ curl（冒烟用） ---
  sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends libkeyutils1 libsasl2-2 libldap-common curl >/dev/null

  # --- 2) Python 依赖：先钉 mcp 基线版本，再装仓库内 core 源码 → 驱动 whl → 项目 → pytest ---
  # pytest-cov：项目 pyproject 的 addopts 带 --cov（官方 09 §9.3 标准），pytest 启动
  # 即需要该插件，缺了直接 unrecognized arguments 报错；容器内跑 --no-cov（见下），
  # 这里补装是双保险（插件缺失与覆盖率统计两个故障面分开消除）。
  pip install --no-cache-dir -q -i "$TUNA" "mcp==2.3.0" "sqlparse==0.6.0" "pydantic==2.14.0"
  pip install --no-cache-dir -q -i "$TUNA" "$CORE_DIR"
  pip install --no-cache-dir -q --no-index "$DRIVER_WHL"
  pip install --no-cache-dir -q -i "$TUNA" "pytest==9.1.1" "pytest-asyncio==1.4.0" "pytest-cov==7.1.0" "uvicorn==0.54.0"
  pip install --no-cache-dir -q -i "$TUNA" "$SERVER_DIR"

  # 驱动 .so 的运行时搜索路径
  SITE=$(python -c 'import site; print(site.getsitepackages()[0])')
  export LD_LIBRARY_PATH="$SITE/gaussdb_psycopg2.lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

  # --- 3) 凭证组装：密码只在容器文件系统内流转 ---
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

  # --- 4) 真库 live 测试（repo 挂载只读：禁 pyc/pytest cache） ---
  # --no-cov 覆盖 addopts 的 --cov：包以 site-packages 方式安装，--cov=src 会测出
  # 0% 并触发 cov-fail-under=80 误报；容器轨不做覆盖率统计。
  cd "$SERVER_DIR"
  export PYTHONDONTWRITEBYTECODE=1
  GAUSSDB_DATA_MCP_CREDENTIALS_FILE="$CRED" \
    python -m pytest tests/integration -m live -v -p no:cacheprovider --no-cov

  # --- 5) streamable-http 冒烟：Bearer 鉴权 + initialize ---
  printf '%s\n' '{"tokens": {"smoke": "tok_live_1"}}' > "$TOKENS"
  chmod 600 "$TOKENS"

  GAUSSDB_DATA_MCP_CREDENTIALS_FILE="$CRED" \
  GAUSSDB_DATA_MCP_TOKENS_FILE="$TOKENS" \
  MCP_SERVER_HOST=127.0.0.1 \
    mcp_server_gaussdb_data -t streamable-http &
  SERVER_PID=$!
  trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

  INIT_BODY='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"1.0"}}}'
  probe() {
    curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8000/mcp \
      -H 'Content-Type: application/json' \
      -H 'Accept: application/json, text/event-stream' \
      -d "$INIT_BODY"
  }

  # 等服务就绪（curl 连不上时 %{http_code} 为 000）
  for _ in $(seq 1 60); do
    code=$(probe || true)
    [ "$code" != "000" ] && break
    sleep 0.5
  done

  no_token=$(probe)
  with_token=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8000/mcp \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -H 'Authorization: Bearer tok_live_1' \
    -d "$INIT_BODY")
  echo "smoke: no-token=$no_token with-token=$with_token"
  [ "$no_token" = "401" ] || { echo "FAIL: expected 401 without token, got $no_token"; exit 1; }
  [ "$with_token" = "200" ] || { echo "FAIL: expected 200 with token, got $with_token"; exit 1; }

  echo "LIVE SUITE OK"
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
    bash /repo/server/mcp_server_gaussdb_data/tests/integration/run_live.sh
fi
