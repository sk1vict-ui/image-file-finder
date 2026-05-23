@echo off
REM ============================================================
REM  캡쳐 이미지 원본 파일 찾기 - 포터블 실행 스크립트
REM ============================================================
cd /d "%~dp0"
set PYTHONHOME=
set PYTHONPATH=

echo Streamlit 서버를 시작합니다... 브라우저가 자동으로 열립니다.
echo (종료하려면 이 창을 닫으세요)
echo.

start "" "http://localhost:8501"

"%~dp0python\python.exe" -m streamlit run "%~dp0app.py" ^
    --server.address 127.0.0.1 ^
    --server.port 8501 ^
    --server.headless true ^
    --browser.gatherUsageStats false

pause
