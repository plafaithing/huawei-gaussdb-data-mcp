#!/usr/bin/env sh
set -eu
PACKAGE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# ---- GaussDB 官方驱动 whl 前置检查：架构一致性校验（防 ARM 机器装 x86 whl）----
# wheels/ 同放两架构 whl（多架构交付）时也只装当前机器这一份；有 whl 但 arch
# 不匹配即失败退出（装错架构 whl 会在 import 期报错，先 fail-fast 指明原因）。
MACHINE=$(uname -m 2>/dev/null || true)
DRIVER_WHL=""
MISMATCH=""
for f in "$PACKAGE_DIR"/wheels/GaussDB_Kernel_*-py311-none-linux_*.whl; do
  [ -f "$f" ] || continue
  case "$f" in
    *linux_x86_64.whl)  whl_arch=x86_64 ;;
    *linux_aarch64.whl) whl_arch=aarch64 ;;
    *)                  whl_arch=unknown ;;
  esac
  if [ "$whl_arch" = "$MACHINE" ]; then
    DRIVER_WHL=$f
  elif [ -z "$DRIVER_WHL" ]; then
    MISMATCH="$MISMATCH $(basename "$f")"
  fi
done

if [ -n "$DRIVER_WHL" ]; then
  echo "driver whl for $MACHINE: $(basename "$DRIVER_WHL")"
elif [ -n "$MISMATCH" ]; then
  echo "ERROR: driver whl arch mismatch: machine=$MACHINE but wheels/ has:$MISMATCH" >&2
  echo "       Rerun ./fetch-driver.sh with the official driver package matching this" >&2
  echo "       machine's OS/arch (see README §2.3); a wrong-arch whl cannot be installed." >&2
  exit 1
else
  echo "NOTE: no GaussDB_Kernel driver whl in wheels/ — the psycopg2 track stays" >&2
  echo "      unavailable until you run ./fetch-driver.sh (see README §2.3); the" >&2
  echo "      pyodbc track (Windows native) is unaffected." >&2
fi

if command -v uv >/dev/null 2>&1; then
  uv venv "$PACKAGE_DIR/.venv"
  uv pip install --python "$PACKAGE_DIR/.venv/bin/python" \
    "$PACKAGE_DIR/wheels/huaweicloud_mcp_core-1.1.1-py3-none-any.whl" "$PACKAGE_DIR"
  # 可选：安装 GaussDB 官方驱动（存在且架构匹配才装；仅 Linux/py3.11，见 README）
  if [ -n "$DRIVER_WHL" ]; then
    uv pip install --python "$PACKAGE_DIR/.venv/bin/python" "$DRIVER_WHL" &&
      echo "driver installed; set LD_LIBRARY_PATH (see README 安装节)"
  fi
else
  python3 -m venv "$PACKAGE_DIR/.venv"
  "$PACKAGE_DIR/.venv/bin/pip" install "$PACKAGE_DIR/wheels/huaweicloud_mcp_core-1.1.1-py3-none-any.whl" "$PACKAGE_DIR"
  if [ -n "$DRIVER_WHL" ]; then
    "$PACKAGE_DIR/.venv/bin/pip" install "$DRIVER_WHL"
  fi
fi
"$PACKAGE_DIR/.venv/bin/gaussdb-data-mcp" --help >/dev/null
printf '%s\n' "Installed successfully. Next: cp credentials.example.json credentials.json (chmod 600)."
