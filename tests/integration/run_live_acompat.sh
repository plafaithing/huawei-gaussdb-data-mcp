#!/usr/bin/env bash
# A 兼容专项 live runner（宿主机入口，自包含：容器内再执行自身）。
#
# 用法（仓库根，默认指向 GY1 分布式实例）：
#   bash gaussdb-data-mcp/tests/integration/run_live_acompat.sh
# 覆盖：LIVE_HOST / LIVE_PASSWORD_JSON（容器内路径，默认 /repo/ops/create-gy1.json）
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
  SERVER_DIR="$REPO/gaussdb-data-mcp"
  CORE_WHL="$SERVER_DIR/wheels/huaweicloud_mcp_core-1.1.1-py3-none-any.whl"
  DRIVER_WHL="$REPO/ops/driver-unpack/python-driver/python_driver/Euler2.10/GaussDB_Kernel_507_0_0-2.9.10-py311-none-linux_x86_64.whl"
  TUNA=https://pypi.tuna.tsinghua.edu.cn/simple
  CRED=/tmp/live-credentials.json
  ACRED=/tmp/live-credentials-acompat.json

  # --- 1) 系统依赖 + Python 依赖（同 run_live.sh） ---
  sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends libkeyutils1 libsasl2-2 libldap-common >/dev/null
  pip install --no-cache-dir -q -i "$TUNA" "mcp==2.3.0" "sqlparse==0.6.0" "pydantic==2.14.0"
  pip install --no-cache-dir -q -i "$TUNA" "$CORE_WHL"
  pip install --no-cache-dir -q --no-index "$DRIVER_WHL"
  pip install --no-cache-dir -q -i "$TUNA" "pytest==9.1.1" "pytest-asyncio==1.4.0"
  pip install --no-cache-dir -q -i "$TUNA" "$SERVER_DIR"

  SITE=$(python -c 'import site; print(site.getsitepackages()[0])')
  export LD_LIBRARY_PATH="$SITE/gaussdb_psycopg2.lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

  # --- 2) 主凭证（postgres 库）：默认 GY1 分布式（LIVE_* 可覆盖） ---
  python -c '
import json, os
# 注意 get 的 default 只在键缺失时生效；宿主段可能传空串，用 or 兜底默认值
with open(os.environ.get("LIVE_PASSWORD_JSON") or "/repo/ops/create-gy1.json", encoding="utf-8") as f:
    password = json.load(f)["password"]
creds = {
    "host": os.environ.get("LIVE_HOST") or "101.245.89.231",
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
    python -m pytest tests/integration/test_live_acompat.py -m live -v -p no:cacheprovider

  # --- 5) 清理探测库 ---
  GAUSSDB_DATA_MCP_CREDENTIALS_FILE="$CRED" \
    python tests/integration/acompat_provision.py drop

  echo "ACOMPAT LIVE SUITE OK"
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
    bash /repo/gaussdb-data-mcp/tests/integration/run_live_acompat.sh
fi
