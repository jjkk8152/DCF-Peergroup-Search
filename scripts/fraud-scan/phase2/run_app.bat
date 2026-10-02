@echo off
setlocal
rem NOTE: keep lines above "chcp 65001" ASCII-only (UTF-8, no BOM, CRLF).
for /f "tokens=2 delims=:." %%a in ('chcp') do set "_OLDCP=%%a"
chcp 65001 >nul

rem Phase 2 (External Fraud Benchmarking & RCM Coverage) Streamlit 앱 실행 — Python 3.10 이상 필요
pushd "%~dp0..\..\.."
where python >nul 2>nul
if errorlevel 1 (
  echo [오류] Python 이 설치되어 있지 않습니다. https://www.python.org 에서 설치하세요.
  goto :end
)
python -c "import streamlit, pandas, openpyxl" 2>nul
if errorlevel 1 python -m pip install -r scripts\fraud-scan\phase2\requirements.txt
python -m streamlit run scripts\fraud-scan\phase2\app.py

:end
popd
pause
chcp %_OLDCP% >nul
endlocal
