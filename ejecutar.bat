@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
set HF_HUB_DISABLE_XET=1
set TOKENIZERS_PARALLELISM=false

REM ===== Tuning del retriever v4 =====
REM Se pueden cambiar antes de cada ejecucion
set TOP_K_FAISS=30
set TOP_K_FTS=20
set TOP_K_FINAL=10
set USAR_RERANKER=1
REM set SCORE_MIN_RERANK=-2.0    (descomentar para activar threshold "no se")

echo =========================================================
echo   GACETA UMSA v4 - Solo base v2 (chunks_v2 + e5-base)
echo                  + Hybrid FAISS+FTS5
echo                  + Reranker BGE
echo                  + Prompt v2 (citas, orden invertido)
echo =========================================================
echo.
echo Asegurate de haber generado la base antes:
echo   generar_base.bat
echo.

"%~dp0venv\Scripts\python.exe" -u "%~dp0consultar_gaceta.py"

pause
