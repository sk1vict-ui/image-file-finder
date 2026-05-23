@echo off
REM ============================================================
REM  캡쳐 이미지 원본 파일 찾기 - Windows 포터블 빌드 스크립트
REM ============================================================
REM  이 스크립트는 Windows에서 실행하세요.
REM  실행하면 dist\CaptureFinder_Portable\ 폴더가 생성되며,
REM  그 폴더를 USB나 다른 PC에 통째로 옮겨도 작동합니다 (Python 설치 불필요).
REM ============================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

set PY_VERSION=3.11.9
set PY_EMBED_URL=https://www.python.org/ftp/python/%PY_VERSION%/python-%PY_VERSION%-embed-amd64.zip
set GETPIP_URL=https://bootstrap.pypa.io/get-pip.py
set DIST_DIR=dist\CaptureFinder_Portable
set PY_DIR=%DIST_DIR%\python
set DOWNLOAD_DIR=build_cache

echo.
echo [1/7] 작업 폴더 준비...
if exist "%DIST_DIR%" rd /s /q "%DIST_DIR%"
mkdir "%DIST_DIR%" 2>nul
mkdir "%PY_DIR%" 2>nul
mkdir "%DOWNLOAD_DIR%" 2>nul

echo.
echo [2/7] Python embeddable %PY_VERSION% 다운로드...
if not exist "%DOWNLOAD_DIR%\python-embed.zip" (
    powershell -Command "Invoke-WebRequest -Uri '%PY_EMBED_URL%' -OutFile '%DOWNLOAD_DIR%\python-embed.zip'"
    if errorlevel 1 ( echo Python 다운로드 실패 & exit /b 1 )
)

echo.
echo [3/7] Python 압축 해제...
powershell -Command "Expand-Archive -Force '%DOWNLOAD_DIR%\python-embed.zip' '%PY_DIR%'"

echo.
echo [4/7] pip 활성화 (python311._pth 수정)...
REM embeddable Python은 기본적으로 site-packages가 비활성화되어 있으므로 활성화
powershell -Command "$f=Get-ChildItem '%PY_DIR%\python*._pth' | Select-Object -First 1; (Get-Content $f.FullName) -replace '#import site','import site' | Set-Content $f.FullName"

echo.
echo [5/7] get-pip.py 다운로드 및 pip 설치...
if not exist "%DOWNLOAD_DIR%\get-pip.py" (
    powershell -Command "Invoke-WebRequest -Uri '%GETPIP_URL%' -OutFile '%DOWNLOAD_DIR%\get-pip.py'"
)
"%PY_DIR%\python.exe" "%DOWNLOAD_DIR%\get-pip.py" --no-warn-script-location
if errorlevel 1 ( echo pip 설치 실패 & exit /b 1 )

echo.
echo [6/7] 의존성 패키지 설치 (수 분 소요)...
"%PY_DIR%\python.exe" -m pip install --no-warn-script-location -r requirements.txt
if errorlevel 1 ( echo 패키지 설치 실패 & exit /b 1 )

echo.
echo [7/7] 앱 파일 및 실행 스크립트 복사...
copy /Y app.py "%DIST_DIR%\app.py" >nul
if exist .streamlit (
    xcopy /E /I /Y .streamlit "%DIST_DIR%\.streamlit" >nul
)
copy /Y run.bat "%DIST_DIR%\run.bat" >nul

echo.
echo ============================================================
echo  ✅ 빌드 완료!
echo.
echo  결과 폴더:  %DIST_DIR%\
echo  실행 방법:  %DIST_DIR%\run.bat 더블클릭
echo.
echo  이 폴더 전체를 USB / 다른 PC에 복사해서 사용 가능합니다.
echo  (대상 PC에 Python 설치 없이도 작동)
echo.
echo  💡 .ppt / .doc 레거시 Office 파일도 처리하고 싶다면,
echo     대상 PC에 LibreOffice를 추가로 설치하세요 (선택사항).
echo     .pptx / .docx는 LibreOffice 없이도 작동합니다.
echo ============================================================
pause
