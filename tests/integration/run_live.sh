#!/usr/bin/env bash
# Live 集成测试 runner（宿主机入口，自包含：容器内再执行自身）。
#
# 用法（仓库根）：
#   bash gaussdb-data-mcp/tests/integration/run_live.sh
#
# 职责（详见同目录 README.md）：
#   1) 在 python:3.11-slim 容器内：清华源 apt/pip 装官方自研驱动 whl（Linux/py311
#      专用）+ 项目 + pytest（套路同 ops/run_driver_poc_container.sh）
#   2) 从挂载的 /repo/ops/create-bj4.json 读实例管理员密码，组装 0600 的
#      /tmp/live-credentials.json（密码不进命令行、不回显）
#   3) 跑 pytest tests/integration -m live（真库端到端，7 工具全覆盖）
#   4) streamable-http 冒烟：无 token 401 / 带 Bearer token initialize 200
set -eu

if [ -d /repo ]; then
  # ==================== 容器内段 ====================
  REPO=/repo
  SERVER_DIR="$REPO/gaussdb-data-mcp"
  CORE_WHL="$SERVER_DIR/wheels/huaweicloud_mcp_core-1.1.1-py3-none-any.whl"
  DRIVER_WHL="$REPO/ops/driver-unpack/python-driver/python_driver/Euler2.10/GaussDB_Kernel_507_0_0-2.9.10-py311-none-linux_x86_64.whl"
  TUNA=https://pypi.tuna.tsinghua.edu.cn/simple
  CRED=/tmp/live-credentials.json
  TOKENS=/tmp/live-tokens.json

  # --- 1) 系统依赖：自研驱动的间接链接依赖（keyutils/sasl/ldap）+ curl（冒烟用） ---
  sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends libkeyutils1 libsasl2-2 libldap-common curl >/dev/null

  # --- 2) Python 依赖：先钉 mcp 基线版本，再装本地 core whl → 驱动 whl → 项目 → pytest ---
  pip install --no-cache-dir -q -i "$TUNA" "mcp==2.3.0" "sqlparse==0.6.0" "pydantic==2.14.0"
  pip install --no-cache-dir -q -i "$TUNA" "$CORE_WHL"
  pip install --no-cache-dir -q --no-index "$DRIVER_WHL"
  pip install --no-cache-dir -q -i "$TUNA" "pytest==9.1.1" "pytest-asyncio==1.4.0" "uvicorn==0.54.0"
  pip install --no-cache-dir -q -i "$TUNA" "$SERVER_DIR"

  # 驱动 .so 的运行时搜索路径（同 ops/run_driver_poc_container.sh）
  SITE=$(python -c 'import site; print(site.getsitepackages()[0])')
  export LD_LIBRARY_PATH="$SITE/gaussdb_psycopg2.lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

  # --- 3) 凭证组装：密码只在容器文件系统内流转 ---
  # LIVE_HOST / LIVE_PASSWORD_JSON 可覆盖（默认北京四集中式；分布式跑法见 README）
  python -c '
import json, os
with open(os.environ.get("LIVE_PASSWORD_JSON") or "/repo/ops/create-bj4.json", encoding="utf-8") as f:
    password = json.load(f)["password"]
creds = {
    "host": os.environ.get("LIVE_HOST") or "1.94.240.147",
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
  cd "$SERVER_DIR"
  export PYTHONDONTWRITEBYTECODE=1
  GAUSSDB_DATA_MCP_CREDENTIALS_FILE="$CRED" \
    python -m pytest tests/integration -m live -v -p no:cacheprovider

  # --- 5) streamable-http 冒烟：Bearer 鉴权 + initialize ---
  printf '%s\n' '{"tokens": {"smoke": "tok_live_1"}}' > "$TOKENS"
  chmod 600 "$TOKENS"

  GAUSSDB_DATA_MCP_CREDENTIALS_FILE="$CRED" \
  GAUSSDB_DATA_MCP_TOKENS_FILE="$TOKENS" \
  MCP_SERVER_HOST=127.0.0.1 \
    gaussdb-data-mcp -t streamable-http &
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
  exec docker run --rm \
    -v "$REPO_ROOT:/repo:ro" \
    -e LIVE_HOST="${LIVE_HOST:-}" \
    -e LIVE_PASSWORD_JSON="${LIVE_PASSWORD_JSON:-}" \
    -w /repo/gaussdb-data-mcp \
    python:3.11-slim \
    bash /repo/gaussdb-data-mcp/tests/integration/run_live.sh
fi
