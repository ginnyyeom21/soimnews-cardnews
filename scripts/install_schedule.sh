#!/bin/bash
# 매일 오전 11시(로컬 시간) 자동 실행을 macOS launchd에 등록/해제합니다.
#   등록: ./scripts/install_schedule.sh install
#   해제: ./scripts/install_schedule.sh uninstall
#   상태: ./scripts/install_schedule.sh status
#   즉시 1회 실행(TEST-01 강제 트리거): ./scripts/install_schedule.sh trigger
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="net.socialimpactnews.cardnews"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$PROJECT_DIR/state/logs"

case "${1:-}" in
  install)
    mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PROJECT_DIR}/run.sh</string>
  </array>
  <key>WorkingDirectory</key><string>${PROJECT_DIR}</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>11</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key><string>${LOG_DIR}/launchd.out.log</string>
  <key>StandardErrorPath</key><string>${LOG_DIR}/launchd.err.log</string>
  <key>RunAtLoad</key><false/>
</dict>
</plist>
PLISTEOF
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo "등록 완료: 매일 11:00 → $PLIST"
    echo "주의: 11시에 Mac이 꺼져 있으면 실행되지 않습니다. 상시 실행이 필요하면 서버/클라우드 크론을 쓰세요."
    ;;
  uninstall)
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "해제 완료"
    ;;
  status)
    launchctl list | grep "$LABEL" || echo "등록되어 있지 않습니다."
    ;;
  trigger)
    launchctl start "$LABEL" && echo "즉시 실행을 요청했습니다. 로그: $LOG_DIR/launchd.out.log"
    ;;
  *)
    echo "사용법: $0 {install|uninstall|status|trigger}" >&2
    exit 1
    ;;
esac
