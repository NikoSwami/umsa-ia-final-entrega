@echo off
setlocal EnableExtensions
title Consulta a la Gaceta IA - Web (React + FastAPI)
chcp 65001 >nul 2>&1

REM ============================================================
REM   Forzar el directorio actual al del .bat (sirve para doble clic)
REM ============================================================
cd /d "%~dp0"

set "PY=%~dp0venv\Scripts\python.exe"

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
set HF_HUB_DISABLE_XET=1
set TOKENIZERS_PARALLELISM=false

REM Tuning del retriever
set TOP_K_FAISS=30
set TOP_K_FTS=20
set TOP_K_FINAL=10
set USAR_RERANKER=1

REM Limpiar herencia de venvs anteriores
set PYTHONPATH=
set PYTHONHOME=
set "VIRTUAL_ENV=%~dp0venv"

REM Node.js portatil incluido en la carpeta del proyecto (carpeta node\)
set "PATH=%~dp0node;%~dp0venv\Scripts;%PATH%"

echo ============================================================
echo   CONSULTA A LA GACETA IA - Interfaz Web
echo   React + Vite + Tailwind  ^|  Backend FastAPI
echo ============================================================
echo.

REM ============================================================
REM   1) Verificar el venv local de Python
REM ============================================================
if not exist "%PY%" (
    echo [ERROR] No se encontro python.exe en el venv local:
    echo    %PY%
    echo.
    echo Crea el venv con:  python -m venv "%~dp0venv"
    goto :fin
)
echo [OK] Python del venv encontrado.

REM ============================================================
REM   2) Verificar Node.js portatil (carpeta node\ del proyecto)
REM ============================================================
if not exist "%~dp0node\node.exe" (
    echo.
    echo [ERROR] No se encontro Node.js portatil en:
    echo    %~dp0node\
    echo.
    echo La carpeta 'node' debe viajar junto al resto del proyecto.
    echo.
    goto :fin
)
for /f "delims=" %%v in ('"%~dp0node\node.exe" --version') do echo [OK] Node.js portatil %%v

REM ============================================================
REM   3) Verificar FastAPI y Uvicorn en el venv
REM ============================================================
echo Verificando FastAPI y Uvicorn...
"%PY%" -c "import fastapi, uvicorn" >nul 2>&1
if errorlevel 1 (
    echo [+] Instalando FastAPI y Uvicorn en el venv...
    "%PY%" -m pip install fastapi "uvicorn[standard]"
    if errorlevel 1 (
        echo [ERROR] Fallo la instalacion de FastAPI/Uvicorn.
        goto :fin
    )
)
echo [OK] FastAPI y Uvicorn disponibles.
echo.

REM ============================================================
REM   4) Instalar dependencias del frontend (solo la primera vez)
REM ============================================================
pushd "%~dp0frontend"

if not exist "node_modules" (
    echo [+] Instalando dependencias del frontend ^(npm install^).
    echo     La primera vez puede tardar varios minutos...
    call npm install
    if errorlevel 1 (
        echo [ERROR] Fallo 'npm install'.
        popd
        goto :fin
    )
)

REM ============================================================
REM   5) Compilar la interfaz React
REM ============================================================
echo [+] Compilando la interfaz ^(npm run build^)...
call npm run build
if errorlevel 1 (
    echo [ERROR] Fallo 'npm run build'.
    popd
    goto :fin
)
popd
echo [OK] Interfaz compilada en frontend\dist.
echo.

REM ============================================================
REM   6) Lanzar el servidor (sirve la API y la interfaz)
REM ============================================================
echo ============================================================
echo   Lanzando servidor en http://127.0.0.1:8000
echo   El modelo de IA se carga en segundo plano (puede tardar).
echo   ^(Ctrl+C en esta ventana para detener^)
echo ============================================================
echo.

REM El navegador lo abre ahora api_gaceta.py automaticamente (usar --no-browser para evitarlo).
"%PY%" "%~dp0api_gaceta.py" --host 127.0.0.1 --port 8000

set "RC=%ERRORLEVEL%"
echo.
echo ============================================================
echo   api_gaceta.py termino con codigo: %RC%
echo ============================================================

:fin
echo.
echo Presiona una tecla para cerrar esta ventana.
pause >nul
endlocal
