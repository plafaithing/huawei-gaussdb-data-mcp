#!/usr/bin/env bash
# fetch-driver.sh —— 从官方 GaussDB 驱动包自动提取 Python 驱动 whl 到 wheels/
#
# 官方驱动不发布 PyPI，只随驱动包（DBS-GaussDB-driver_*.tar.gz，约 1.6GB）按
# 部署形态 / OS 变体 / CPU 架构分发。本脚本层层降级探测输入，流式解包（不把
# 大包整体展开），把目标 whl 放进 wheels/（先删同架构旧 whl——各 OS 变体的
# whl 内容不同、文件名相同，混装会装错）。
#
# 实测包内结构（507.0.0 mini x86 包，2026-10）：
#   DBS-GaussDB-driver_*.tar.gz
#     └── Centralized|Distributed|CloudNative|GaussStor/python_driver.tar.gz
#           └── python_driver/<Variant>_<X86_64|ARM64>/GaussDB-Kernel_*_Python_*.tar.gz
#                 └── GaussDB_Kernel_507_0_0-2.9.10-py311-none-linux_<arch>.whl
#   变体目录实测（x86 包）：Hce2.0 / Euler2.10 / Euler2.5 / Suse15.7 / Suse2.5
#
# 用法：
#   ./fetch-driver.sh /path/to/DBS-GaussDB-driver_*.tar.gz        # 官方外层包
#   ./fetch-driver.sh /path/to/python_driver.tar.gz               # 已解包的形态 mini 包
#   ./fetch-driver.sh /path/to/GaussDB-Kernel_*_Python_*.tar.gz   # 单 OS 变体包
#   ./fetch-driver.sh /path/to/GaussDB_Kernel_*-py311-none-linux_*.whl  # 直接给 whl
#
# 环境变量（可选）：
#   FETCH_DRIVER_SHAPE    部署形态：Centralized（默认）/ Distributed / CloudNative
#   FETCH_DRIVER_VARIANT  OS 变体目录名（如 Hce2.0 / Euler2.10 / Suse15.7），
#                         设置后跳过 /etc/os-release 自动映射
#
# 流程：uname -m + /etc/os-release → 变体目录 → 逐层 tar -xzOf 流式提取 →
# wheels/GaussDB_Kernel_*_py311-none-linux_<arch>.whl。幂等可重入，临时目录即用即删。
set -eu

PACKAGE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
WHEELS_DIR="$PACKAGE_DIR/wheels"

die() { printf 'fetch-driver: ERROR: %s\n' "$1" >&2; exit 1; }
log() { printf 'fetch-driver: %s\n' "$1"; }

[ "${1:-}" ] || die "usage: $0 /path/to/DBS-GaussDB-driver_*.tar.gz (or python_driver.tar.gz / per-OS tarball / whl)"
[ -f "$1" ] || die "input not found: $1"
SRC=$1

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

# ---- 目标架构：uname -m → 驱动包变体目录后缀 + whl 平台标签 ------------------
case "$(uname -m)" in
  x86_64)          ARCH_SUFFIX="X86_64"; WHL_ARCH="x86_64" ;;
  aarch64 | arm64) ARCH_SUFFIX="ARM64";  WHL_ARCH="aarch64" ;;
  *) die "unsupported machine arch: $(uname -m) (need x86_64 or aarch64)" ;;
esac
log "target arch: $ARCH_SUFFIX (whl tag: linux_$WHL_ARCH)"

# ---- OS 变体：/etc/os-release 的 ID/VERSION_ID → 包内变体目录前缀 ------------
detect_variant_prefix() {
  local id="" ver=""
  if [ -r /etc/os-release ]; then
    id=$(. /etc/os-release && printf '%s' "${ID:-}")
    ver=$(. /etc/os-release && printf '%s' "${VERSION_ID:-}")
  fi
  case "$id" in
    hce)                     printf 'Hce%s' "$ver" ;;
    openeuler | euleros)     printf 'Euler%s' "$ver" ;;
    sles | suse | opensuse*) printf 'Suse%s' "$ver" ;;
    *)                       printf '' ;;
  esac
}
VARIANT_PREFIX=${FETCH_DRIVER_VARIANT:-$(detect_variant_prefix)}
if [ "$VARIANT_PREFIX" ]; then
  log "OS variant prefix: $VARIANT_PREFIX"
else
  log "OS variant unknown (no /etc/os-release mapping); will list available variants"
fi

# ---- 降级探测循环：whl 就收工，否则找下一层 tar.gz 流式解到临时目录 ----------
# 每层 tar -tzf 全量列表（流式，不解数据）、tar -xzOf 提取单成员（流式），
# 磁盘只多占一份被提取的小文件。最多下钻 3 层（外层包 → python_driver → 单 OS 包）。
DIRECT_WHL=""
case "$SRC" in
  *.whl) DIRECT_WHL=1; whl_member=$SRC ;;
  *) whl_member="" ;;
esac

cur=$SRC
i=0
while [ "$i" -lt 3 ] && [ -z "$whl_member" ]; do
  i=$((i + 1))
  listing=$(tar -tzf "$cur") || die "cannot read tar.gz: $cur"

  # 本层已直接含目标 whl？（单 OS 变体包 / 中间层都可能）
  whl_member=$(printf '%s\n' "$listing" |
    grep -E "GaussDB_Kernel_.*-py311-none-linux_${WHL_ARCH}\.whl$" | head -n 1 || true)

  if [ "$whl_member" ]; then
    log "found whl member: $whl_member"
    break
  fi

  # 下一层候选 tar.gz 成员
  next_member=""
  if printf '%s\n' "$listing" | grep -qE '(^|/)python_driver\.tar\.gz$'; then
    # 外层官方包：按部署形态挑 python_driver.tar.gz（实测包内 4 个形态各一份）
    shape=${FETCH_DRIVER_SHAPE:-Centralized}
    next_member=$(printf '%s\n' "$listing" |
      grep -E "(^|/)${shape}/python_driver\.tar\.gz$" | head -n 1 || true)
    if [ ! "$next_member" ]; then
      available=$(printf '%s\n' "$listing" | grep -E '(^|/)[^/]+/python_driver\.tar\.gz$' |
        sed -E 's#^.*/([^/]+)/python_driver\.tar\.gz$#\1#' | sort -u | tr '\n' ' ')
      [ "$available" ] || die "no python_driver.tar.gz inside $cur"
      die "no python_driver.tar.gz under shape '$shape' in $cur; available shapes: $available (set FETCH_DRIVER_SHAPE)"
    fi
  elif [ "$VARIANT_PREFIX" ]; then
    # python_driver.tar.gz / 变体集合包：变体_架构 → 变体_任意后缀 → 变体 目录
    next_member=$(printf '%s\n' "$listing" |
      grep -E "/${VARIANT_PREFIX}_${ARCH_SUFFIX}/[^/]*\.tar\.gz$" | head -n 1 || true)
    [ "$next_member" ] || next_member=$(printf '%s\n' "$listing" |
      grep -E "/${VARIANT_PREFIX}_[A-Za-z0-9]+/[^/]*\.tar\.gz$" | head -n 1 || true)
    [ "$next_member" ] || next_member=$(printf '%s\n' "$listing" |
      grep -E "/${VARIANT_PREFIX}/[^/]*\.tar\.gz$" | head -n 1 || true)
  fi

  if [ -z "$next_member" ]; then
    # 映射不到变体：列出本层可用的变体目录让用户选（交互）或退出（非交互）
    variants=$(printf '%s\n' "$listing" | grep -E '/[^/]+/[^/]+\.tar\.gz$' |
      sed -E 's#^(.*)/([^/]+)/[^/]+\.tar\.gz$#\2#' | sort -u || true)
    [ "$variants" ] || die "no nested tar.gz and no target whl inside $cur; cannot proceed"
    printf 'fetch-driver: no variant matched (%s_%s). Available variants:\n' \
      "${VARIANT_PREFIX:-<unknown OS>}" "$ARCH_SUFFIX" >&2
    printf '%s\n' "$variants" >&2
    if [ -t 0 ] && [ -z "${FETCH_DRIVER_VARIANT:-}" ]; then
      printf 'fetch-driver: type one variant name to continue: ' >&2
      read -r pick
      [ "$pick" ] || die "empty selection"
      VARIANT_PREFIX=$pick
      i=$((i - 1))  # 同层重试
      continue
    fi
    die "set FETCH_DRIVER_VARIANT=<name> and rerun"
  fi

  log "extracting (streamed): $next_member"
  extracted="$TMP_DIR/level$i.tar.gz"
  tar -xzOf "$cur" "$next_member" > "$extracted" ||
    die "failed to extract member '$next_member' from $cur"
  cur=$extracted
done

if [ -z "$whl_member" ]; then
  die "no GaussDB_Kernel_*-py311-none-linux_${WHL_ARCH}.whl found inside $SRC (3 levels deep)"
fi

# ---- 落盘 wheels/：先删同架构旧 whl（各 OS 变体文件名相同内容不同）------------
whl_name=$(basename "$whl_member")
mkdir -p "$WHEELS_DIR"
rm -f "$WHEELS_DIR"/GaussDB_Kernel_*-py311-none-linux_"$WHL_ARCH".whl \
  "$WHEELS_DIR"/"$whl_name"
if [ "$DIRECT_WHL" ]; then
  cp "$SRC" "$WHEELS_DIR/$whl_name"
else
  tar -xzOf "$cur" "$whl_member" > "$WHEELS_DIR/$whl_name" ||
    die "failed to write $WHEELS_DIR/$whl_name"
fi
log "installed: wheels/$whl_name"

other=$(ls "$WHEELS_DIR" | grep -E '^GaussDB_Kernel_.*\.whl$' | grep -vF "$whl_name" | tr '\n' ' ' || true)
[ "$other" ] && log "kept other-arch whl: $other (multi-arch wheels/ supported)"

cat <<EOF

Done. Next steps (see README §2.3):
  1. pip uninstall psycopg2 psycopg2-binary   # 官方驱动要求先清开源 psycopg2
  2. pip install $WHEELS_DIR/$whl_name
  3. export LD_LIBRARY_PATH=/usr/local/lib/python3.11/site-packages/gaussdb_psycopg2.lib
EOF
