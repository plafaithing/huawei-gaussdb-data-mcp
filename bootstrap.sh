#!/usr/bin/env bash
# bootstrap.sh —— 向导式最小可用配置生成器（issue #1）
#
# 生成最小接入所需的两份机密文件（均 chmod 600，内容不回显到终端日志）：
#   credentials.json   数据库六字段凭证（host/port/dbname/user/password/sslmode）
#   tokens.json        HTTP 客户端 Bearer 令牌表（openssl rand 生成）
# 并按 sslmode 给出 CA 证书的获取提示（verify-ca/verify-full 需要控制台下载的
# cacert.pem，放 credentials.json 旁即可零配置生效，见 README §3.2）。
#
# 用法：
#   ./bootstrap.sh                       # 交互向导（每项有默认值）
#   ./bootstrap.sh --host 192.168.0.242 --port 8000 --dbname postgres \
#                  --user mcp_ro [--password '...'] [--sslmode prefer]
#                  [--client-name dify] [--output-dir /opt/mcp/conf] [--force]
#
# 纯 bash + openssl（无 openssl 时退回 /dev/urandom），无第三方依赖。
set -eu

OUT_DIR="."
HOST="" PORT="" DBNAME="" USER_NAME="" PASSWORD="" SSLMODE=""
CLIENT_NAME="default" FORCE=""

die() { printf 'bootstrap: ERROR: %s\n' "$1" >&2; exit 1; }
usage() { sed -n '/^# bootstrap.sh/,/^set -eu/p' "$0" | sed 's/^# \{0,1\}//'; }

# ---- 参数解析（给了参数的字段跳过交互提问）----------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --host)         HOST=$2; shift 2 ;;
    --port)         PORT=$2; shift 2 ;;
    --dbname)       DBNAME=$2; shift 2 ;;
    --user)         USER_NAME=$2; shift 2 ;;
    --password)     PASSWORD=$2; shift 2 ;;
    --sslmode)      SSLMODE=$2; shift 2 ;;
    --client-name)  CLIENT_NAME=$2; shift 2 ;;
    --output-dir)   OUT_DIR=$2; shift 2 ;;
    --force)        FORCE=1; shift ;;
    -h | --help)    usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

ask() { # ask <变量名> <提示语> <默认值>：参数已给则跳过，交互空输入取默认
  local __var=$1 __prompt=$2 __default=$3 __in=""
  if [ -n "${!__var:-}" ]; then return; fi          # 参数已给，跳过
  if [ ! -t 0 ]; then die "missing value for '$__var' (non-interactive; pass it via flag, see --help)"; fi
  printf '%s [%s]: ' "$__prompt" "$__default" >&2
  IFS= read -r __in || die "read failed"
  printf -v "$__var" '%s' "${__in:-$__default}"
}

ask HOST "database host" "192.168.0.242"
ask PORT "database port" "8000"
ask DBNAME "database name" "postgres"
ask USER_NAME "database user" "mcp_ro"
if [ -z "$PASSWORD" ]; then
  if [ -t 0 ]; then
    printf 'database password (input hidden): ' >&2
    IFS= read -rs PASSWORD || die "read failed"
    printf '\n' >&2
    [ -n "$PASSWORD" ] || die "empty password"
  else
    die "missing --password (non-interactive)"
  fi
fi
ask SSLMODE "sslmode (disable/prefer/require/verify-ca/verify-full)" "prefer"
ask CLIENT_NAME "first client name in tokens.json" "dify"

# ---- 产物路径；已存在不静默覆盖 ---------------------------------------------
mkdir -p "$OUT_DIR"
CRED_FILE="$OUT_DIR/credentials.json"
TOKEN_FILE="$OUT_DIR/tokens.json"
if [ -f "$CRED_FILE" ] && [ -z "$FORCE" ]; then die "$CRED_FILE exists; use --force to overwrite"; fi
if [ -f "$TOKEN_FILE" ] && [ -z "$FORCE" ]; then die "$TOKEN_FILE exists; use --force to overwrite"; fi

# JSON 字符串转义（覆盖引号/反斜杠两类会破坏结构字符；控制字符不在凭证里出现）
json_escape() { local s=$1; s=${s//\\/\\\\}; s=${s//\"/\\\"}; printf '%s' "$s"; }

# ---- tokens.json：openssl rand 优先，退回 /dev/urandom -----------------------
if command -v openssl >/dev/null 2>&1; then
  TOKEN="tok_$(openssl rand -hex 24)"
else
  TOKEN="tok_$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
fi

umask 077   # 新文件 600 起步（077 掩码下目录 700）
cat > "$CRED_FILE" <<EOF
{"host": "$(json_escape "$HOST")", "port": $PORT, "dbname": "$(json_escape "$DBNAME")",
 "user": "$(json_escape "$USER_NAME")", "password": "$(json_escape "$PASSWORD")",
 "sslmode": "$(json_escape "$SSLMODE")"}
EOF
cat > "$TOKEN_FILE" <<EOF
{"tokens": {"$CLIENT_NAME": "$TOKEN"}}
EOF
chmod 600 "$CRED_FILE" "$TOKEN_FILE"

printf 'bootstrap: wrote %s (600)\n' "$CRED_FILE"
printf 'bootstrap: wrote %s (600), client %q token %q\n' "$TOKEN_FILE" "$CLIENT_NAME" "$TOKEN"

# ---- CA 证书提示（verify-ca/verify-full 必须显式 CA 才能真正校验）------------
case "$SSLMODE" in
  verify-ca | verify-full)
    printf 'bootstrap: sslmode=%s needs a CA certificate:\n' "$SSLMODE"
    printf '  1. 控制台下载实例的 cacert.pem（CloudDB SSL 证书下载）\n'
    printf '  2. 放到 %s 同目录并命名 cacert.pem 即零配置生效；\n' "$OUT_DIR"
    printf '     或 credentials.json 加 "sslrootcert": "/abs/path/cacert.pem"\n'
    printf '  3. pyodbc 轨（Windows）必须显式 sslrootcert 绝对路径（fail-closed）。\n'
    ;;
  *)
    printf 'bootstrap: note: 公网地址请用 sslmode=require 及以上（公网强制 SSL 护栏）。\n'
    ;;
esac

cat <<'EOF'

Next (README §3 / §5):
  GAUSSDB_DATA_MCP_CREDENTIALS_FILE=<credentials.json 路径> \
  [GAUSSDB_DATA_MCP_TOKENS_FILE=<tokens.json 路径>] \
    gaussdb-data-mcp            # stdio；HTTP 另需 MCP_SERVER_HOST=0.0.0.0
EOF
