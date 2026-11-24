@echo off
chcp 65001 >nul
rem KR Public Data Hub 켜기 (Windows: 이 파일을 더블클릭). 이 창을 닫으면 꺼진다.
cd /d "%~dp0"
curl -s -o nul http://127.0.0.1:8765/api/status && (start "" http://127.0.0.1:8765 & exit /b 0)
python -c "import fastapi, uvicorn, httpx, openpyxl, anthropic" 2>nul || python -m pip install --user -r requirements.txt
if not exist data\catalog.sqlite python -m datahub.build_catalog
echo KR Public Data Hub를 켜는 중입니다...
start "" /b python -m datahub.server >> data\server.log 2>&1
set /a n=0
:wait
set /a n+=1
if %n% equ 60 echo 색인을 읽느라 아직 켜는 중입니다. 켜지면 브라우저가 저절로 열립니다. 이 창은 닫지 마세요.
if %n% gtr 600 (echo 10분이 지나도 응답이 없습니다. data\server.log 를 보세요. & pause & exit /b 1)
timeout /t 1 >nul
curl -s -o nul http://127.0.0.1:8765/api/status || goto wait
start "" http://127.0.0.1:8765
echo 켜졌습니다: http://127.0.0.1:8765  (이 창을 닫으면 꺼집니다)
pause >nul
