@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
set HF_HUB_DISABLE_XET=1
set TOKENIZERS_PARALLELISM=false

echo =========================================================
echo   GENERAR BASE - Construir base (chunks + embeddings)
echo =========================================================
echo.
echo Modo recomendado en orden:
echo   1) Test:       python gaceta_rag\generar_base.py --limit 100 --solo-chunks --reset
echo   2) Chunks:     python gaceta_rag\generar_base.py --reset --solo-chunks
echo   3) Embeddings: python gaceta_rag\generar_base.py --solo-embed
echo.
echo O completo en una pasada:
echo   python gaceta_rag\generar_base.py
echo.
echo Argumentos utiles:
echo   --limit N        procesar solo N docs (test)
echo   --max-ocr-mb 3   skip docs con OCR mayor a 3 MB (default 3)
echo   --modelo e5-small   modelo mas chico (~470 MB)
echo   --modelo e5-large   modelo mas grande (~2.2 GB, mejor calidad)
echo   --reset          borrar chunks_v2.sqlite antes de empezar
echo   --reanudar       saltar docs ya en SQLite
echo.
pause
echo.
echo Ejecutando pipeline completo (todos los docs, modelo e5-base)...
echo.

"%~dp0venv\Scripts\python.exe" -u "%~dp0gaceta_rag\generar_base.py" --reset

pause
