#!/usr/bin/env sh
set -eu
PACKAGE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export GAUSSDB_DATA_MCP_READ_ONLY="${GAUSSDB_DATA_MCP_READ_ONLY:-true}"
[ -f "$PACKAGE_DIR/credentials.json" ] || { echo "credentials.json missing (cp credentials.example.json)" >&2; exit 1; }
export GAUSSDB_DATA_MCP_CREDENTIALS_FILE="${GAUSSDB_DATA_MCP_CREDENTIALS_FILE:-$PACKAGE_DIR/credentials.json}"
exec "$PACKAGE_DIR/.venv/bin/gaussdb-data-mcp" "$@"
