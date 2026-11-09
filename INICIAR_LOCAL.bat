@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Studio Debora Ventura

echo ==========================================
echo   STUDIO DEBORA VENTURA - VERSAO 1.0.1
echo ==========================================

if not exist ".venv\Scripts\python.exe" (
  echo Criando ambiente virtual...
  py -m venv .venv
  if errorlevel 1 (
    echo ERRO: Nao foi possivel criar o ambiente virtual.
    pause
    exit /b 1
  )
)

call ".venv\Scripts\activate.bat"
echo Python em uso:
python --version
echo.
echo Instalando dependencias...
python -m pip install --upgrade pip
if errorlevel 1 goto :erro
python -m pip install -r requirements.txt
if errorlevel 1 goto :erro

if not exist "data" mkdir data

echo.
echo ==========================================
echo Sistema disponivel em http://127.0.0.1:8000
echo Mantenha esta janela aberta enquanto usar.
echo Para encerrar, pressione CTRL+C.
echo ==========================================
echo.
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
if errorlevel 1 goto :erro
goto :fim

:erro
echo.
echo ERRO: Nao foi possivel iniciar o sistema.
pause
exit /b 1

:fim
pause
