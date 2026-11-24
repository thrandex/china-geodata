#!/usr/bin/env bash
# Build geosite-cn.dat and geosite-cn-enhanced.dat into OUT_DIR, run the
# quality gates, and write the .sha256sum files, manifest.json and README.md.
#
# Usage: scripts/build.sh OUT_DIR
#
# By default the inputs are downloaded: the latest v2fly/domain-list-community
# release (dlc.dat, checked against dlc.dat.sha256sum) and four lists from the
# current Loyalsoldier/v2ray-rules-dat release commit.
#
# Offline / reproducible build from inputs you already have:
#   INPUT_DIR=dir DLC_TAG=<v2fly tag> LS_COMMIT=<40-hex sha> scripts/build.sh OUT_DIR
# INPUT_DIR must hold dlc.dat, dlc.dat.sha256sum and the four .txt lists.
#
# GITHUB_TOKEN, if set, is sent to api.github.com to avoid rate limits.
set -euo pipefail

OUT=${1:?usage: scripts/build.sh OUT_DIR}
SCRIPTS=$(cd "$(dirname "$0")" && pwd)
LISTS=(direct-list.txt china-list.txt apple-cn.txt google-cn.txt)
PURE=geosite-cn.dat
ENHANCED=geosite-cn-enhanced.dat
PURE_MIN=5000
ENHANCED_MIN=80000
COVERED=taobao.com,douyin.com,alicdn.com,bilibili.com,qq.com,baidu.com
NOT_COVERED=tiktok.com,tiktokv.com,capcut.com,github.com,google.com,youtube.com,openai.com

die() { echo "error: $*" >&2; exit 1; }

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

fetch() { # fetch URL FILE
  curl -fsSL --proto '=https' --proto-redir '=https' --retry 3 --retry-delay 5 \
    --connect-timeout 20 --max-time 300 -o "$2" "$1"
  [ -s "$2" ] || die "empty download: $1"
}

api() { # api PATH -> JSON on stdout
  local auth=()
  if [ -n "${GITHUB_TOKEN:-}" ]; then auth=(-H "Authorization: Bearer $GITHUB_TOKEN"); fi
  curl -fsS --proto '=https' --retry 3 --retry-delay 5 --connect-timeout 20 --max-time 60 \
    -H 'Accept: application/vnd.github+json' ${auth[@]+"${auth[@]}"} "https://api.github.com/$1"
}

json_get() { python3 -I -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }

# OUT_DIR must be new or empty: everything in it gets published.
if [ -e "$OUT" ] && [ -n "$(ls -A "$OUT")" ]; then die "$OUT is not empty"; fi
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)

# Downloads are untrusted: keep them in their own new directory.
WORK=$(mktemp -d "${TMPDIR:-/tmp}/china-geodata.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
IN="$WORK/in"
mkdir "$IN"

if [ -n "${INPUT_DIR:-}" ]; then
  : "${DLC_TAG:?DLC_TAG is required with INPUT_DIR}" "${LS_COMMIT:?LS_COMMIT is required with INPUT_DIR}"
  for f in dlc.dat dlc.dat.sha256sum "${LISTS[@]}"; do cp "$INPUT_DIR/$f" "$IN/$f"; done
else
  DLC_TAG=$(api repos/v2fly/domain-list-community/releases/latest | json_get tag_name)
  LS_COMMIT=$(api repos/Loyalsoldier/v2ray-rules-dat/commits/release | json_get sha)
fi
[[ $DLC_TAG =~ ^[A-Za-z0-9._-]+$ ]] || die "bad v2fly tag: $DLC_TAG"
[[ $LS_COMMIT =~ ^[0-9a-f]{40}$ ]] || die "bad Loyalsoldier commit: $LS_COMMIT"
echo "v2fly release: $DLC_TAG"
echo "Loyalsoldier release commit: $LS_COMMIT"

if [ -z "${INPUT_DIR:-}" ]; then
  U=https://github.com/v2fly/domain-list-community/releases/download/$DLC_TAG
  fetch "$U/dlc.dat" "$IN/dlc.dat"
  fetch "$U/dlc.dat.sha256sum" "$IN/dlc.dat.sha256sum"
  for f in "${LISTS[@]}"; do
    fetch "https://raw.githubusercontent.com/Loyalsoldier/v2ray-rules-dat/$LS_COMMIT/$f" "$IN/$f"
  done
fi

want=$(cut -d' ' -f1 "$IN/dlc.dat.sha256sum")
got=$(sha256 "$IN/dlc.dat")
[[ $want =~ ^[0-9a-f]{64}$ ]] || die "dlc.dat.sha256sum is malformed"
[ "$want" = "$got" ] || die "dlc.dat sha256 mismatch: $got != $want"
echo "dlc.dat sha256 ok: $got"

# Pure: the v2fly CN tag, byte-for-byte.
python3 -I "$SCRIPTS/build_geosite.py" \
  --in "$IN/dlc.dat" --categories CN --source-tag "$DLC_TAG" \
  --check-covered "$COVERED" \
  --check-not-covered "$NOT_COVERED" \
  --out "$OUT/$PURE" \
  --sha256-file "$OUT/$PURE.sha256sum" \
  --manifest "$WORK/manifest-cn.txt"

# Enhanced: v2fly CN + Loyalsoldier lists, one tag CN.
python3 -I "$SCRIPTS/build_geosite.py" \
  --in "$IN/dlc.dat" --categories CN --source-tag "$DLC_TAG" \
  --merge-into CN --dedupe \
  --add-list "$IN/direct-list.txt" --add-list "$IN/china-list.txt" \
  --add-list "$IN/apple-cn.txt" --add-list "$IN/google-cn.txt" \
  --lists-ref "Loyalsoldier/v2ray-rules-dat release@$LS_COMMIT" \
  --drop-domain top \
  --check-covered taobao.com,alicdn.com,douyin.com,douyinvod.com,amemv.com,zjcdn.com,bilibili.com,163.com,iqiyi.com,qq.com,baidu.com,aliyuncs.com,queniukw.com \
  --check-covered example.cn,example.wang,example.在线,example.中国 \
  --check-not-covered "$NOT_COVERED" \
  --check-not-covered example.top \
  --out "$OUT/$ENHANCED" \
  --sha256-file "$OUT/$ENHANCED.sha256sum" \
  --manifest "$WORK/manifest-cn-enhanced.txt"

# Gates (one tag CN, minimum sizes) and manifest.json.
BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
list_args=()
for f in "${LISTS[@]}"; do list_args+=(--list "$IN/$f"); done
python3 -I "$SCRIPTS/manifest.py" --out-dir "$OUT" \
  --dlc "$IN/dlc.dat" --dlc-tag "$DLC_TAG" --ls-commit "$LS_COMMIT" "${list_args[@]}" \
  --output "$PURE:$PURE_MIN" --output "$ENHANCED:$ENHANCED_MIN" \
  --build-time "$BUILD_TIME"

cat > "$OUT/README.md" <<EOF
# china-geodata release

Built $BUILD_TIME by GitHub Actions from the \`main\` branch. This branch is
replaced by a single new commit on every build.

| File | Content |
|---|---|
| \`$PURE\` | v2fly/domain-list-community $DLC_TAG, tag \`CN\`, byte-for-byte |
| \`$ENHANCED\` | v2fly \`CN\` + Loyalsoldier/v2ray-rules-dat (release@${LS_COMMIT:0:12}) direct-list, china-list, apple-cn, google-cn; one tag \`CN\` |

Each \`.dat\` has a \`.sha256sum\` file. \`manifest.json\` records the sources,
their sha256 and the rule counts.

Data: v2fly/domain-list-community (MIT) and Loyalsoldier/v2ray-rules-dat
(GPL-3.0). Released under GPL-3.0.
EOF

echo "outputs in $OUT:"
ls -l "$OUT"
