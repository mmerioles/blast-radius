#!/bin/sh
# Run the selector locally (no Docker) against a sample dir and score it.
# usage: tools/run_local.sh <venv_python> <sqlglot_root> <maps.pickle> [sample_dir]
PY=$1; ROOT=$2; MAPS=$3; SAMPLE=${4:-sample}
OUT=$(mktemp)
BR_DATA=$SAMPLE BR_ROOT=$ROOT BR_MAPS=$MAPS BR_WORK=${TMPDIR:-/tmp}/brwork PATH=$(dirname $PY):$PATH \
  /usr/bin/time -p $PY selector.py > $OUT
python3 score.py $SAMPLE $SAMPLE/answers.json $OUT
