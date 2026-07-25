#!/bin/bash
# 카드 렌더링 → 이미지 저장소 push → 인스타그램 발행을 순서대로 실행한다.
#
# Graph API는 공개 URL로만 이미지를 받으므로, 발행보다 push가 먼저여야 한다.
# run.sh를 그냥 실행하면 렌더 직후 발행이 일어나 아직 안 올라간 URL을 넘기게 된다.
#
# 사용법:
#   ./scripts/publish.sh --limit 1     # 1건만 발행 (첫 시험용)
#   ./scripts/publish.sh               # 기본 10건
set -euo pipefail

cd "$(dirname "$0")/.."
OUT_DIR="out"
RUN_ID="run-$(date +%Y%m%d-%H%M%S)"

echo "▶ 실행 ID: $RUN_ID"
echo

# ── 1. 렌더링만 (발행 없음) ─────────────────────────────────
echo "① 카드 렌더링 (발행 없음)"
./run.sh --dry-run --run-id "$RUN_ID" "$@"
echo

RUN_PATH="$OUT_DIR/$RUN_ID"
[ -d "$RUN_PATH" ] || { echo "렌더링 결과가 없습니다: $RUN_PATH"; exit 1; }
COUNT=$(find "$RUN_PATH" -name '*.png' | wc -l | tr -d ' ')
[ "$COUNT" -gt 0 ] || { echo "카드 이미지가 없습니다."; exit 1; }
echo "  카드 ${COUNT}장 렌더링됨"
echo

# ── 2. 이미지 저장소에 push ─────────────────────────────────
echo "② 이미지 저장소에 업로드"
(
  cd "$OUT_DIR"
  git add -A
  if git diff --cached --quiet; then
    echo "  변경 없음 (이미 올라가 있음)"
  else
    git commit -q -m "카드 이미지: $RUN_ID"
    git push -q origin main
    echo "  push 완료"
  fi
)
echo

# ── 3. 공개 URL이 실제로 열리는지 확인 ──────────────────────
BASE=$(grep -E '^PUBLIC_IMAGE_BASE_URL=' config/settings.env | head -1 | cut -d= -f2- | tr -d ' ')
if [ -n "$BASE" ]; then
  SAMPLE=$(find "$RUN_PATH" -name '*_01.png' | head -1)
  REL="${SAMPLE#$OUT_DIR/}"
  URL="${BASE%/}/$REL"
  echo "③ 공개 URL 확인: $REL"
  for i in 1 2 3 4 5 6; do
    CODE=$(curl -s -o /dev/null -w '%{http_code}' -L --max-time 15 "$URL" || echo 000)
    [ "$CODE" = "200" ] && { echo "  HTTP 200 · 공개 확인"; break; }
    echo "  HTTP $CODE · 반영 대기 중... ($i/6)"
    sleep 10
  done
  [ "${CODE:-000}" = "200" ] || {
    echo
    echo "  이미지가 아직 공개되지 않아 발행을 중단합니다."
    echo "  이 상태로 발행하면 인스타그램이 이미지를 못 가져와 실패합니다."
    echo "  잠시 뒤 다시 실행하세요: ./scripts/publish.sh $*"
    exit 1
  }
  echo
fi

# ── 4. 발행 ─────────────────────────────────────────────────
echo "④ 인스타그램 발행"
./run.sh --run-id "$RUN_ID" "$@"
