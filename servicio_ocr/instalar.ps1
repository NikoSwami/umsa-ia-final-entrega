# Instala el entorno del servicio OCR
#
#   servicio_ocr\instalar.ps1            # motor LIVIANO (ONNX) en servicio_ocr\.venv
#   servicio_ocr\instalar.ps1 -Vl        # PaddleOCR-VL (CPU) en servicio_ocr\.venv-vl
#   servicio_ocr\instalar.ps1 -Vl -Gpu   # PaddleOCR-VL con GPU NVIDIA (CUDA 12.6)
#
# Requiere Python 3.12 de 64 bits (py -3.12). Rutas cortas a proposito: Paddle
# trae archivos con rutas muy largas que fallan en carpetas profundas.
param([switch]$Vl, [switch]$Gpu)
$ErrorActionPreference = "Stop"

$aqui = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = if ($Vl) { Join-Path $aqui ".venv-vl" } else { Join-Path $aqui ".venv" }
$py = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "Creando el entorno en $venv"
    py -3.12 -m venv $venv
}
& $py -m pip install --upgrade pip --quiet

if ($Vl) {
    $req = Join-Path $aqui "requirements-vl.txt"
    if ($Gpu) {
        # Paddle GPU no esta en PyPI. El CDN de Baidu corta descargas largas:
        # timeouts largos y reintentos.
        & $py -m pip install --timeout 180 --retries 10 paddlepaddle-gpu==3.2.1 `
            -i https://www.paddlepaddle.org.cn/packages/stable/cu126/
        $tmp = Join-Path $env:TEMP "req_ocr_vl_gpu.txt"
        Get-Content $req | Where-Object { $_ -notmatch '^paddlepaddle==' } | Set-Content -Encoding utf8 $tmp
        $req = $tmp
    }
    & $py -m pip install --timeout 180 --retries 10 -r $req
    $motor = "vl"
} else {
    & $py -m pip install --timeout 180 --retries 10 -r (Join-Path $aqui "requirements.txt")
    & $py -m pip install --no-deps -r (Join-Path $aqui "requirements-sin-deps.txt")
    $motor = "liviano"
}

Write-Host ""
Write-Host "Listo. Para arrancarlo (desde la raiz del repo):"
Write-Host "  `$env:OCR_MOTOR = `"$motor`""
Write-Host "  $py -m uvicorn servicio_ocr.app:app --host 127.0.0.1 --port 8001"
