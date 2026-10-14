#!/usr/bin/env sh
set -eu
PACKAGE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if command -v uv >/dev/null 2>&1; then
  uv venv "$PACKAGE_DIR/.venv"
  uv pip install --python "$PACKAGE_DIR/.venv/bin/python" \
    "$PACKAGE_DIR/wheels/huaweicloud_mcp_core-1.1.1-py3-none-any.whl" "$PACKAGE_DIR"
  # 可选：安装 GaussDB 官方驱动（存在才装；仅 Linux/py3.11，见 README）
  ls "$PACKAGE_DIR"/wheels/GaussDB_Kernel_*.whl >/dev/null 2>&1 && \
    uv pip install --python "$PACKAGE_DIR/.venv/bin/python" "$PACKAGE_DIR"/wheels/GaussDB_Kernel_*.whl && \
    echo "driver installed; set LD_LIBRARY_PATH (see README 安装节)"
else
  python3 -m venv "$PACKAGE_DIR/.venv"
  "$PACKAGE_DIR/.venv/bin/pip" install "$PACKAGE_DIR/wheels/huaweicloud_mcp_core-1.1.1-py3-none-any.whl" "$PACKAGE_DIR"
  ls "$PACKAGE_DIR"/wheels/GaussDB_Kernel_*.whl >/dev/null 2>&1 && \
    "$PACKAGE_DIR/.venv/bin/pip" install "$PACKAGE_DIR"/wheels/GaussDB_Kernel_*.whl
fi
"$PACKAGE_DIR/.venv/bin/gaussdb-data-mcp" --help >/dev/null
printf '%s\n' "Installed successfully. Next: cp credentials.example.json credentials.json (chmod 600)."
