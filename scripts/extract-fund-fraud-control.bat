@echo off
setlocal
rem ---------------------------------------------------------------------------
rem  NOTE: keep everything above "chcp 65001" ASCII-only. This file is UTF-8
rem  (no BOM, CRLF); cmd reads lines before chcp in the system code page.
rem ---------------------------------------------------------------------------
for /f "tokens=2 delims=:." %%a in ('chcp') do set "_OLDCP=%%a"
chcp 65001 >nul

rem ===========================================================================
rem  자금부정통제 공시 추출 (OpenDART) - 윈도우 실행기
rem   - 더블클릭: 회사 목록 파일을 메모장으로 열어 편집한 뒤 실행
rem   - 목록 파일(.txt/.csv)을 이 bat 위로 드래그앤드롭: 그 파일로 바로 실행
rem   - 필요: Node.js 18 이상, OpenDART API 키
rem   - 결과: fund-fraud-control-output\날짜_시각\ 폴더 (summary.csv / result.md / result.json)
rem ===========================================================================
title 자금부정통제 공시 추출 - OpenDART

rem 저장소 루트로 이동 (이 파일은 scripts\ 아래에 있음)
pushd "%~dp0.." || goto :fail

rem ─── 1. Node.js 확인 ───
where node >nul 2>nul
if errorlevel 1 (
  echo [오류] Node.js가 설치되어 있지 않습니다.
  echo        https://nodejs.org 에서 LTS 버전을 설치한 뒤 다시 실행하세요.
  goto :fail
)
set "_NODEMAJOR=0"
for /f "tokens=1 delims=v." %%v in ('node -v') do set "_NODEMAJOR=%%v"
if %_NODEMAJOR% LSS 18 (
  echo [오류] Node.js 18 이상이 필요합니다. 현재 버전: v%_NODEMAJOR%
  goto :fail
)

rem ─── 2. 의존성 설치 (최초 1회) ───
if not exist "node_modules\axios\" goto :install
if not exist "node_modules\adm-zip\" goto :install
goto :deps_ok
:install
echo [준비] 최초 실행 - 의존성 패키지를 설치합니다. 몇 분 걸릴 수 있습니다...
call npm ci --no-audit --no-fund || call npm install --no-audit --no-fund
if errorlevel 1 (
  echo [오류] 패키지 설치에 실패했습니다. 인터넷 연결을 확인하세요.
  goto :fail
)
:deps_ok

rem ─── 3. OpenDART API 키 ───
rem 스크립트는 .env.local 이 있으면 그것만, 없으면 .env 를 읽는다 (같은 규칙으로 확인)
if defined OPENDART_API_KEY goto :key_ok
set "_ENVFILE="
if exist ".env" set "_ENVFILE=.env"
if exist ".env.local" set "_ENVFILE=.env.local"
if defined _ENVFILE findstr /r /c:"^ *OPENDART_API_KEY *=" "%_ENVFILE%" >nul 2>nul && goto :key_ok

echo.
echo OpenDART API 키가 필요합니다. 발급: https://opendart.fss.or.kr  [인증키 신청/관리]
set /p "OPENDART_API_KEY=API 키 입력: "
if not defined OPENDART_API_KEY (
  echo [오류] API 키가 입력되지 않았습니다.
  goto :fail
)
set "OPENDART_API_KEY=%OPENDART_API_KEY: =%"
choice /c YN /n /m "이 키를 .env.local 에 저장해 다음부터 자동으로 쓸까요? [Y/N] "
if errorlevel 2 goto :key_ok
(echo.& echo OPENDART_API_KEY=%OPENDART_API_KEY%)>>".env.local"
echo [저장] .env.local
:key_ok

rem ─── 4. 회사 목록 ───
set "_WORKDIR=%CD%\fund-fraud-control-output"
if not exist "%_WORKDIR%\" mkdir "%_WORKDIR%"

if "%~1"=="" goto :default_list
if /i "%~x1"==".xlsx" goto :xlsx
if /i "%~x1"==".xls" goto :xlsx
set "_INPUT=%~f1"
goto :input_ready

:default_list
set "_INPUT=%_WORKDIR%\companies.txt"
if not exist "%_INPUT%" (
  copy /y "scripts\fund-fraud-companies.example.txt" "%_INPUT%" >nul
  goto :edit_list
)
echo.
echo 회사 목록 파일: %_INPUT%
choice /c YN /n /m "회사 목록을 수정할까요? [Y/N] "
if errorlevel 2 goto :input_ready
:edit_list
echo.
echo 메모장에서 회사 목록을 입력하고 저장하세요.
echo 저장이 끝나면 이 창으로 돌아와 아무 키나 누르세요...
start "" notepad "%_INPUT%"
pause >nul
:input_ready

rem ─── 5. 사업연도 범위 ───
echo.
set "_FROM="
set "_TO="
set /p "_FROM=시작 사업연도 [Enter=2023]: "
set /p "_TO=종료 사업연도 [Enter=작년]: "
set "_YEARARGS="
if defined _FROM set "_YEARARGS=--from %_FROM%"
if defined _TO set "_YEARARGS=%_YEARARGS% --to %_TO%"

rem ─── 6. 실행 (결과는 실행 시각별 폴더에 저장) ───
set "_TS="
for /f "delims=" %%t in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss" 2^>nul') do set "_TS=%%t"
if not defined _TS set "_TS=latest"
set "_OUT=%_WORKDIR%\%_TS%"

echo.
call npx --yes tsx "scripts\extract-fund-fraud-control.ts" --input "%_INPUT%" %_YEARARGS% --out "%_OUT%"
if errorlevel 1 goto :fail

echo.
echo 결과 폴더: %_OUT%
echo   summary.csv  - 회사/연도별 공시 여부 요약, 엑셀로 열기
echo   result.md    - 추출 원문 리포트
echo   result.json  - 전체 데이터
if exist "%_OUT%\" start "" "%_OUT%"
set "_RC=0"
goto :end

:xlsx
echo [오류] 엑셀 파일은 바로 읽을 수 없습니다.
echo        엑셀에서 [다른 이름으로 저장] - [CSV (쉼표로 분리)] 로 저장한 뒤 그 파일을 끌어다 놓으세요.
goto :fail

:fail
echo.
echo [실패] 위 메시지를 확인하세요.
set "_RC=1"

:end
popd
echo.
pause
chcp %_OLDCP% >nul
endlocal & exit /b %_RC%
