#!/usr/bin/env bash
# DGXSparkUtil モニタリング API を起動する
# 起動:  ./run.sh            (フォアグラウンド)
#        ./run.sh -d         (バックグラウンド/nohup、ログは api/server.log)
set -euo pipefail

cd "$(dirname -- "${BASH_SOURCE[0]}")"

PORT="${PORT:-8080}"
HOST="${HOST:-0.0.0.0}"

if [ ! -d venv ]; then
  python3 -m venv venv
  ./venv/bin/pip install -q -r requirements.txt
fi

if [ "${1:-}" = "-d" ]; then
  nohup ./venv/bin/python -m uvicorn main:app --host "$HOST" --port "$PORT" \
    >> server.log 2>&1 &
  echo "started (pid $!) on port $PORT, log: $PWD/server.log"
else
  exec ./venv/bin/python -m uvicorn main:app --host "$HOST" --port "$PORT"
fi
