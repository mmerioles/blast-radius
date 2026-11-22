#!/bin/sh
# usage: tools/evalrun.sh <py> <root> <maps> <dir>   -> score + misses detail
PY=$1; ROOT=$2; MAPS=$3; DIR=$4
OUT=$(mktemp); ERR=$(mktemp)
BR_DATA=$DIR BR_ROOT=$ROOT BR_MAPS=$MAPS BR_WORK=${TMPDIR:-/tmp}/brwork PATH=$(dirname $PY):$PATH $PY selector.py > $OUT 2> $ERR
grep -E "done in|fallback|error" $ERR | grep -v "^  child"
python3 score.py $DIR $DIR/answers.json $OUT | head -2
python3 - $DIR $OUT <<'PY'
import json, sys
d, out = sys.argv[1], sys.argv[2]
ts = json.load(open(d + "/tests.json"))["tests"]
a = json.load(open(d + "/answers.json"))["commits"]
for line in open(out):
    r = json.loads(line); sel = {i for w in r["workers"] for i in w}; f = set(a[r["commit"]]["failing"])
    if f - sel:
        print("MISS", r["commit"], len(f), len(sel), [ts[i]["id"] for i in sorted(f - sel)][:3])
PY
