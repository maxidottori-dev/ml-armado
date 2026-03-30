@echo off
title ML Armado - Procesador
echo.
echo  ================================
echo   ML ARMADO - Iniciando app...
echo  ================================
echo.

cd /d "%~dp0"

echo  Verificando Python...
py --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python no esta instalado.
    echo  Instala Python desde https://python.org
    pause
    exit
)

echo  Instalando/verificando dependencias...
py -m pip install fastapi uvicorn[standard] python-multipart pdfplumber pdf2image Pillow reportlab -q

echo.
echo  Abriendo el browser en 3 segundos...
timeout /t 3 /nobreak >nul
start http://localhost:8000

echo.
echo  Iniciando servidor...
echo  (No cierres esta ventana mientras uses la app)
echo.
py -m uvicorn main:app --host 0.0.0.0 --port 8000

pause
