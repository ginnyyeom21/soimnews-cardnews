#!/bin/bash
# 매일 오전 11시(KST) 정기 실행 진입점. launchd/cron이 이 파일을 호출합니다.
# 수동 실행 예: ./run.sh --dry-run
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="./.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3)"
fi

export PYTHONUNBUFFERED=1
exec "$PYTHON" -m socialcard run "$@"
