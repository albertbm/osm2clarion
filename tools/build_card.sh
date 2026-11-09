#!/bin/bash
# One parameterised command for a card build + its post-card steps (poi_apply, the two
# build_poicap passes, an optional per-unit consent command). Per-unit values (DATA
# VERSION, base image, consent command) and the card naming pattern come from --config,
# not from this script.
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
OSM2CLARION_DEFAULT="$(cd "$HERE/.." && pwd)"

usage() {
  cat <<'EOF'
build_card.sh --config PATH --unit NAME --slug NAME --area NAME[,NAME...] --builddir DIR
              [--osm2clarion PATH] [--number N] [--base IMG] [--what TEXT]
              [--write lev,tiles,addr] [--prev IMG] [--stage DIR]

Builds a card: --landuse area, --house, --coarse-graph, then poi_apply.py and the two
build_poicap.py passes. --config names the build (unit table, areas, cells, data paths,
card naming - see examples/config.json). --osm2clarion defaults to this checkout; pass it
to point the POI steps at a different one. --number is only used in the default --what
text. --prev, if given, is passed to card_check.py as the comparison image. --stage sets
where the intermediate whole-card copies between poi_apply and the two build_poicap passes
go; default is CARDS_DIR/stage (same volume as the card images, for a free cp -c/mv).
BUILDDIR can be on a different, slower or smaller volume - it only holds generated files,
never a whole card.
EOF
}

CONFIG=""; UNIT=""; SLUG=""; AREA=""; OSM2CLARION=""; BUILDDIR=""; NUMBER=""; BASE=""; WHAT=""
WRITE="lev,tiles,addr"; PREV=""; STAGE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2;;
    --unit) UNIT="$2"; shift 2;;
    --slug) SLUG="$2"; shift 2;;
    --area) AREA="$2"; shift 2;;
    --osm2clarion) OSM2CLARION="$2"; shift 2;;
    --builddir) BUILDDIR="$2"; shift 2;;
    --number) NUMBER="$2"; shift 2;;
    --base) BASE="$2"; shift 2;;
    --what) WHAT="$2"; shift 2;;
    --write) WRITE="$2"; shift 2;;
    --prev) PREV="$2"; shift 2;;
    --stage) STAGE="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "unknown arg: $1" >&2; usage; exit 1;;
  esac
done
[ -n "$OSM2CLARION" ] || OSM2CLARION="$OSM2CLARION_DEFAULT"
for req in CONFIG UNIT SLUG AREA BUILDDIR; do
  eval "v=\${$req}"
  [ -n "$v" ] || { echo "missing --${req,,}" >&2; usage; exit 1; }
done

# one field per line, since consent_cmd has spaces; bash 3.2 on macOS has no mapfile
O2C_FIELDS=()
while IFS= read -r o2c_line; do O2C_FIELDS+=("$o2c_line"); done < <(python3 - "$CONFIG" "$UNIT" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
u = cfg['units'][sys.argv[2]]
for v in (cfg['card_naming']['prefix'], u['version'], u.get('base', '-'), u.get('consent_cmd', '-'),
          cfg['poi_python'], cfg['data']['addresses'], cfg['data']['postcodes']):
    print(v)
PY
)
PREFIX="${O2C_FIELDS[0]}" VERSION="${O2C_FIELDS[1]}" CFG_BASE="${O2C_FIELDS[2]}" CONSENT_CMD="${O2C_FIELDS[3]}"
POI_PY="${O2C_FIELDS[4]}" ADDR_CSV="${O2C_FIELDS[5]}" POST_CSV="${O2C_FIELDS[6]}"
expand() { eval echo "$1"; }
[ "$CFG_BASE" = "-" ] && CFG_BASE=""
[ -n "$CFG_BASE" ] && CFG_BASE="$(expand "$CFG_BASE")"
POI_PY="$(expand "$POI_PY")"
ADDR_CSV="$(expand "$ADDR_CSV")"
POST_CSV="$(expand "$POST_CSV")"
[ -n "$BASE" ] || BASE="$CFG_BASE"
[ -n "$WHAT" ] || WHAT="${NUMBER:+$NUMBER: }$SLUG"

export OSM2CLARION
export O2C_CONFIG="$CONFIG"
# fixed FAT write time, so two runs of the same recipe stamp identically; a caller
# wanting a real timestamp in the image sets SOURCE_DATE_EPOCH before calling this.
# 946684800 = 2000-01-01 00:00:00 UTC, safely past the FAT epoch (1980) in every timezone
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-946684800}"
CARDS_DIR="${CARDS_DIR:-$HOME/cards}"
# Default stage dir is under CARDS_DIR, not BUILDDIR: the stage holds whole card copies
# (poi_apply/build_poicap's --base/--out), and cp -c/mv only clone for free on the same
# volume as the base/card images, which live under CARDS_DIR. A BUILDDIR on another
# volume (e.g. scratch space) made every stage copy a real cross-volume copy of a full
# card image and ran a scratch drive out of space.
S="${STAGE:-$CARDS_DIR/stage}"; mkdir -p "$S"

python3 "$HERE/build_gen_lev.py" --config "$CONFIG" --unit "$UNIT" --slug "$SLUG" "$BUILDDIR" --reuse \
  ${BASE:+--base "$BASE"} --area "$AREA" --landuse area --house --write "$WRITE" \
  --coarse-graph --what "$WHAT"

OUT=$(ls -t "$CARDS_DIR"/builds/"$PREFIX"-*-"$UNIT"-"$SLUG".img | head -1)
MANIFEST="${OUT%.img}.manifest.json"
echo "card: $OUT"

export CLARION_DATA_VERSION="$VERSION"
python3 "$HERE/poi_apply.py" --config "$CONFIG" "$BUILDDIR" "$OUT" "$S/poi-$UNIT"
python3 "$OSM2CLARION/tools/buildstamp.py" post-step "$MANIFEST" poi_apply "$BUILDDIR $OUT" "$OUT"

mv "$OUT" "$S/s1-$UNIT.img"
"$POI_PY" "$HERE/build_poicap.py" --config "$CONFIG" --area "$AREA" --allow-placeholder-refs \
  --osm2clarion "$OSM2CLARION" --python "$POI_PY" --base "$S/s1-$UNIT.img" \
  --out "$S/s2-$UNIT.img" --workdir "$S/poicap-$UNIT"
python3 "$OSM2CLARION/tools/buildstamp.py" post-step "$MANIFEST" build_poicap-icons \
  "--area $AREA --allow-placeholder-refs" "$S/s2-$UNIT.img"

"$POI_PY" "$HERE/build_poicap.py" --config "$CONFIG" --area "$AREA" --house-ranges ALL --all-streets \
  --shapes "$BUILDDIR"/rd/*.shapes.json --addresses "$ADDR_CSV" --postcodes "$POST_CSV" \
  --osm2clarion "$OSM2CLARION" --python "$POI_PY" --base "$S/s2-$UNIT.img" \
  --out "$OUT" --workdir "$S/addr-$UNIT"
rm -f "$S/s1-$UNIT.img" "$S/s2-$UNIT.img"
python3 "$OSM2CLARION/tools/buildstamp.py" post-step "$MANIFEST" build_poicap-addr \
  "--area $AREA --house-ranges ALL --all-streets" "$OUT"

if [ "$CONSENT_CMD" != "-" ]; then
  CMD="${CONSENT_CMD//\{img\}/$OUT}"
  eval "$CMD"
  python3 "$OSM2CLARION/tools/buildstamp.py" post-step "$MANIFEST" me0_consent "$CONSENT_CMD" "$OUT"
fi

echo "image sha256:"
shasum -a 256 "$OUT"

CHECKER="$OSM2CLARION/tools/card_check.py"
if [ -f "$CHECKER" ]; then
  if [ -n "$PREV" ]; then
    python3 "$CHECKER" --card "$OUT" --prev "$PREV"
  else
    python3 "$CHECKER" --card "$OUT"
  fi
fi
echo "DONE $UNIT"
