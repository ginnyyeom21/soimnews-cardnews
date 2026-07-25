#!/bin/bash
# 인스타그램 발행 자격증명을 config/settings.env 에 기록한다.
#
# Graph API 탐색기에서 받은 단기 토큰(1~2시간)을 장기 토큰(약 60일)으로 교환하고,
# 페이지에 연결된 인스타그램 계정 ID를 조회해 함께 적는다.
#
# 입력값은 화면에 표시되지 않고, 프로세스 목록에도 남지 않도록 curl 설정을 표준입력으로 넘긴다.
# 실행: ./scripts/setup_instagram.sh
set -euo pipefail

cd "$(dirname "$0")/.."
ENV_FILE="config/settings.env"
API="https://graph.facebook.com/v21.0"
PY="./.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

[ -f "$ENV_FILE" ] || { echo "설정 파일이 없습니다: $ENV_FILE"; exit 2; }

echo "■ Meta 앱 정보 (개발자 콘솔 → 앱 설정 → 기본 설정)"
read -r  -p "  앱 ID          : " APP_ID
# 시크릿과 토큰은 화면에 표시하지 않는다. 다만 입력 여부를 알 수 없으면 불안하므로 글자 수만 알린다.
read -rs -p "  앱 시크릿      : " APP_SECRET; echo "(${#APP_SECRET}자 입력됨)"
echo "■ Graph API 탐색기에서 생성한 사용자 액세스 토큰"
read -rs -p "  단기 토큰      : " SHORT_TOKEN; echo "(${#SHORT_TOKEN}자 입력됨)"
echo

# 붙여넣기에 딸려 오는 공백·줄바꿈을 걷어낸다. 눈에 안 보여서 원인 찾기가 어려운 실패다.
trim() { printf '%s' "$1" | tr -d '[:space:]'; }
APP_ID=$(trim "$APP_ID"); APP_SECRET=$(trim "$APP_SECRET"); SHORT_TOKEN=$(trim "$SHORT_TOKEN")

for v in APP_ID APP_SECRET SHORT_TOKEN; do
  [ -n "${!v}" ] || { echo "값이 비어 있습니다: $v"; exit 2; }
done

case "$APP_ID" in
  *[!0-9]*) echo "앱 ID는 숫자만 있어야 합니다: '$APP_ID'"; exit 2 ;;
esac
[ ${#APP_SECRET} -eq 32 ] || echo "  참고: 앱 시크릿이 ${#APP_SECRET}자입니다(보통 32자). 값을 확인하세요."

# ── 1. 단기 토큰 → 장기 토큰 ────────────────────────────────
echo "① 장기 토큰으로 교환 중..."
LONG_JSON=$(curl -s --config - <<CFG
url = "$API/oauth/access_token"
get
data-urlencode = "grant_type=fb_exchange_token"
data-urlencode = "client_id=$APP_ID"
data-urlencode = "client_secret=$APP_SECRET"
data-urlencode = "fb_exchange_token=$SHORT_TOKEN"
CFG
)

LONG_TOKEN=$("$PY" - "$LONG_JSON" <<'PYEOF'
import json, sys
d = json.loads(sys.argv[1])
if "error" in d:
    e = d["error"]
    sys.stderr.write("  실패: {} (code {})\n".format(e.get("message", ""), e.get("code", "")))
    sys.exit(1)
print(d.get("access_token", ""))
days = int(d.get("expires_in", 0)) // 86400
sys.stderr.write("  성공 · 유효기간 약 {}일\n".format(days) if days else "  성공 · 만료 없음(장기)\n")
PYEOF
) || exit 1
[ -n "$LONG_TOKEN" ] || { echo "  토큰을 받지 못했습니다"; exit 1; }

# ── 2. 인스타그램 계정 확인 ─────────────────────────────────
# 이미 아는 IG_USER_ID가 있으면 그것을 직접 검증한다. 이 경로는 pages_show_list 권한이
# 필요 없다(그 권한은 계정 ID를 '찾기' 위한 것이라, 이미 알고 있으면 쓸 일이 없다).
KNOWN_ID=$(grep -E '^IG_USER_ID=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d ' ')
if [ -n "$KNOWN_ID" ]; then
  echo "② 등록된 인스타그램 계정 ID 검증 중... ($KNOWN_ID)"
  CHECK=$(curl -s --config - <<CFG
url = "$API/$KNOWN_ID"
get
data-urlencode = "fields=username"
data-urlencode = "access_token=$LONG_TOKEN"
CFG
)
  USERNAME=$("$PY" -c "
import json,sys
d=json.loads(sys.argv[1])
print('' if 'error' in d else d.get('username',''))
" "$CHECK")
  if [ -n "$USERNAME" ]; then
    echo "  → @$USERNAME 확인됨"
    IG_ID="$KNOWN_ID"
  else
    echo "  등록된 ID로 조회되지 않습니다. 페이지 목록으로 다시 찾습니다."
  fi
fi

if [ -z "${IG_ID:-}" ]; then
echo "② 페이지에 연결된 인스타그램 계정 조회 중..."
PAGES_JSON=$(curl -s --config - <<CFG
url = "$API/me/accounts"
get
data-urlencode = "fields=name,instagram_business_account{id,username}"
data-urlencode = "access_token=$LONG_TOKEN"
CFG
)

IG_ID=$("$PY" - "$PAGES_JSON" <<'PYEOF'
import json, sys
d = json.loads(sys.argv[1])
if "error" in d:
    e = d["error"]
    sys.stderr.write("  실패: {} (code {})\n".format(e.get("message", ""), e.get("code", "")))
    sys.exit(1)
pages = d.get("data") or []
if not pages:
    sys.stderr.write("  페이지가 하나도 조회되지 않았습니다. pages_show_list 권한을 확인하세요.\n")
    sys.exit(1)
found = None
for p in pages:
    ig = p.get("instagram_business_account")
    mark = "연결됨" if ig else "연결 안 됨"
    sys.stderr.write("  · {:<24} {}\n".format(p.get("name", ""), mark))
    if ig and not found:
        found = ig
if not found:
    sys.stderr.write("  인스타그램 계정이 연결된 페이지가 없습니다.\n")
    sys.exit(1)
sys.stderr.write("  → @{} (ID {})\n".format(found.get("username", "?"), found["id"]))
print(found["id"])
PYEOF
) || exit 1
fi

# ── 3. settings.env 기록 ────────────────────────────────────
echo "③ $ENV_FILE 기록 중..."
CUR_ID=$(grep -E '^IG_USER_ID=' "$ENV_FILE" | head -1 | cut -d= -f2-)
if [ -n "$CUR_ID" ] && [ "$CUR_ID" != "$IG_ID" ]; then
  echo "  주의: 기존 IG_USER_ID($CUR_ID)와 조회 결과($IG_ID)가 다릅니다. 조회 결과로 덮어씁니다."
fi

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
IG_ID="$IG_ID" LONG_TOKEN="$LONG_TOKEN" "$PY" - "$ENV_FILE" > "$TMP" <<'PYEOF'
import os, sys
path = sys.argv[1]
vals = {"IG_USER_ID": os.environ["IG_ID"], "IG_ACCESS_TOKEN": os.environ["LONG_TOKEN"]}
seen = set()
for line in open(path, encoding="utf-8"):
    key = line.split("=", 1)[0].strip() if "=" in line else ""
    if key in vals:
        seen.add(key)
        sys.stdout.write("{}={}\n".format(key, vals[key]))
    else:
        sys.stdout.write(line)
for key, value in vals.items():
    if key not in seen:
        sys.stdout.write("{}={}\n".format(key, value))
PYEOF
cat "$TMP" > "$ENV_FILE"
chmod 600 "$ENV_FILE"

echo
echo "완료했습니다. 토큰은 화면에 표시하지 않고 $ENV_FILE 에만 기록했습니다."
echo "다음으로 상태를 확인하세요:"
echo "  ./.venv/bin/python -m socialcard doctor"
