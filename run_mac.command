#!/bin/bash
# KR Public Data Hub 켜기 (Mac: 이 파일을 더블클릭). 이 창을 닫으면 꺼진다.
cd "$(dirname "$0")" || { echo "프로그램 폴더로 이동하지 못했습니다(폴더 접근 권한 확인)."; read -r -p "엔터를 누르면 닫힙니다"; exit 1; }
PY=python3
export PYTHONDONTWRITEBYTECODE=1   # 프로그램 폴더에 __pycache__ 를 만들지 않음
# 애플 실리콘 Mac에서 터미널이 Rosetta(인텔 방식)로 열려 있으면 파이썬이 x86_64로 돌아
# arm64용 부품(pydantic_core)을 못 읽고 바로 꺼진다(2026-11-13 실제로 발생). 그래서 항상 arm64로 켠다.
if [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then
  PY="arch -arm64 python3"
fi
URL="http://127.0.0.1:8765"
LOG="data/server.log"

# 이미 켜져 있으면 브라우저만 연다
if curl -s -o /dev/null "$URL/api/status"; then
  echo "이미 켜져 있습니다. 브라우저를 엽니다: $URL"
  [ -z "$DATAHUB_NO_BROWSER" ] && open "$URL"
  exit 0
fi

if ! $PY -c "import fastapi, uvicorn, httpx, openpyxl, anthropic" 2>/dev/null; then
  echo "필요한 파이썬 패키지를 설치합니다..."
  $PY -m pip install --user -r requirements.txt || { echo "설치 실패. README의 '시작하기'를 보세요."; read -r -p "엔터를 누르면 닫힙니다"; exit 1; }
fi
if [ ! -f data/catalog.sqlite ]; then
  echo "검색 색인이 없어 처음 만듭니다(1시간 안팎 걸림)..."
  $PY -m datahub.build_catalog
fi

echo "KR Public Data Hub를 켜는 중입니다..."
echo "----- $(date '+%Y-%m-%d %H:%M:%S') 켜기 -----" >> "$LOG"
$PY -m datahub.server >> "$LOG" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null' EXIT INT TERM HUP

# 서버가 실제로 응답할 때까지 기다린 뒤 브라우저를 연다(보통 몇 초, 느린 외장 디스크에서는 몇 분 걸리기도 함).
# 서버가 살아 있는 동안은 끝까지 기다린다.
i=0
while :; do
  if curl -s -m 10 -o /dev/null "$URL/api/status"; then
    echo "켜졌습니다: $URL  (이 창을 닫으면 꺼집니다)"
    [ -z "$DATAHUB_NO_BROWSER" ] && open "$URL"
    wait $PID
    exit 0
  fi
  if ! kill -0 $PID 2>/dev/null; then
    echo "켜지 못했습니다. 기록($LOG)의 마지막 부분:"
    tail -20 "$LOG"
    read -r -p "엔터를 누르면 닫힙니다"
    exit 1
  fi
  i=$((i+1))
  if [ $i -eq 60 ]; then
    echo "색인을 읽느라 아직 켜는 중입니다. 켜지면 브라우저가 저절로 열립니다(이 창은 닫지 마세요)."
  fi
  sleep 1
done
