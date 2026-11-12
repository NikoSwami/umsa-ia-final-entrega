"""
SERVICIO OCR — detras de una API FastAPI
========================================
Un servicio aparte que la Gaceta llama por HTTP (OCR_URL), con uno de dos
motores (OCR_MOTOR):

  liviano  (por defecto) PP-OCRv5 latino + PP-DocLayoutV3 + SLANet+ en ONNX:
           ~15 s y <1 GB por pagina en CPU, tablas incluidas.
  vl       PaddleOCR-VL-1.5, el de PaddleOCR-VL
           google-colab: ~5 min y ~8 GB por pagina en CPU; para la GPU.

Por que aparte y no dentro de api_gaceta.py:
  - Las dependencias del OCR (onnxruntime y opencv; con vl, Paddle y su propio
    numpy) no tienen nada que ver con el chat.
  - La memoria: con vl, ~8 GB mientras lee. En el mismo proceso que e5 + el
    reranker + el indice, un PDF grande podia dejar al chat sin memoria.
  - Se puede correr en otra maquina, o en el contenedor con GPU, sin tocar la
    Gaceta.

Endpoints:
  GET    /salud            -> {listo, cargando, error, modelo, dispositivo, cola}
  POST   /ocr              -> sube un PDF/imagen; 202 {id}
  GET    /ocr/{id}         -> {estado, pagina, paginas, resultado | error}
  DELETE /ocr/{id}         -> libera el resultado
  POST   /ocr/sincrono     -> lo mismo, esperando la respuesta (para curl)

Un PDF largo tarda (liviano: ~15 s por pagina; vl en CPU: ~5 min). Por eso
/ocr devuelve un id enseguida y el cliente pregunta por cual pagina va, en vez
de una peticion que espera todo ese tiempo.

Variables: OCR_MOTOR (liviano|vl), OCR_DPI (200, liviano), OCR_VERSION (v1.5,
vl), OCR_DISPOSITIVO (cpu|gpu, vl), OCR_PRECARGAR (1), OCR_MAX_MB (200).

Arranque:  python -m uvicorn servicio_ocr.app:app --host 0.0.0.0 --port 8001
           (desde la raiz del repo; en Docker lo hace el Dockerfile.ocr)
"""
import asyncio
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from gaceta_rag.carga import ocr as motor_ocr

MAX_MB = float(os.environ.get("OCR_MAX_MB") or 200)
RETENER_S = 3600

_motor = motor_ocr.motor_local()
_trabajos = {}
_lock = threading.Lock()
_cola = []
_hay_trabajo = threading.Event()
_carpeta = Path(tempfile.mkdtemp(prefix="servicio_ocr_"))

def _precargar():
    """El modelo tarda en cargar (y la primera vez, en bajar ~2 GB): mejor al
    arrancar que con el primer PDF esperando."""
    try:
        _motor.cargar()
        print(f"[ocr] {_motor.modelo} listo ({_motor.dispositivo or 'auto'})", flush=True)
    except Exception as exc:
        print(f"[ocr] no se pudo cargar el modelo: {exc}", flush=True)

def _trabajador():
    if (os.environ.get("OCR_PRECARGAR") or "1").strip() not in ("0", "false", "no"):
        _precargar()
    while True:
        _hay_trabajo.wait()
        with _lock:
            if not _cola:
                _hay_trabajo.clear()
                continue
            tid = _cola.pop(0)
            t = _trabajos.get(tid)
        if t is None:
            continue
        t["estado"] = "procesando"

        def avance(hecho, total):
            t["pagina"], t["paginas"] = hecho, total

        try:
            t["resultado"] = _motor.procesar(t["ruta"], paginas_max=t["paginas_max"],
                                             progreso=avance)
            t["estado"] = "listo"
        except Exception as exc:
            t["estado"] = "error"
            t["error"] = str(exc)
        finally:
            t["terminado"] = time.time()
            shutil.rmtree(Path(t["ruta"]).parent, ignore_errors=True)
        _purgar()

def _purgar():
    limite = time.time() - RETENER_S
    with _lock:
        for tid in [k for k, v in _trabajos.items()
                    if v.get("terminado") and v["terminado"] < limite]:
            _trabajos.pop(tid, None)

@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=_trabajador, daemon=True, name="ocr").start()
    yield

app = FastAPI(title="Servicio OCR (PaddleOCR-VL)", lifespan=lifespan)

@app.get("/salud")
def salud():
    with _lock:
        en_cola = len(_cola)
    return dict(_motor.estado(), cola=en_cola)

async def _guardar(archivo: UploadFile) -> Path:
    nombre = Path(str(archivo.filename or "archivo").replace("\\", "/")).name
    if not nombre.lower().endswith(motor_ocr.EXTENSIONES):
        raise HTTPException(status_code=415,
                            detail=f"Formato no soportado: {nombre}. Se aceptan "
                                   f"{', '.join(motor_ocr.EXTENSIONES)}.")
    destino = _carpeta / uuid.uuid4().hex
    destino.mkdir(parents=True)
    ruta = destino / ("entrada" + Path(nombre).suffix.lower())
    total = 0
    with open(ruta, "wb") as f:
        while True:
            trozo = await archivo.read(1024 * 1024)
            if not trozo:
                break
            total += len(trozo)
            if total > MAX_MB * 1024 * 1024:
                shutil.rmtree(destino, ignore_errors=True)
                raise HTTPException(status_code=413,
                                    detail=f"El archivo pasa de {MAX_MB:.0f} MB.")
            f.write(trozo)
    return ruta

@app.post("/ocr", status_code=202)
async def ocr(archivo: UploadFile = File(...), paginas_max: int = Form(0)):
    ruta = await _guardar(archivo)
    tid = uuid.uuid4().hex
    with _lock:
        _trabajos[tid] = {"id": tid, "estado": "en_cola", "ruta": str(ruta),
                          "paginas_max": max(0, int(paginas_max or 0)),
                          "pagina": 0, "paginas": motor_ocr.paginas_de_pdf(ruta),
                          "resultado": None, "error": None, "terminado": None}
        _cola.append(tid)
        _hay_trabajo.set()
    return {"id": tid}

@app.get("/ocr/{tid}")
def ocr_estado(tid: str):
    t = _trabajos.get(tid)
    if t is None:
        raise HTTPException(status_code=404, detail="No existe ese trabajo.")
    return {k: t[k] for k in ("id", "estado", "pagina", "paginas", "resultado", "error")}

@app.delete("/ocr/{tid}")
def ocr_borrar(tid: str):
    with _lock:
        t = _trabajos.pop(tid, None)
        if t and tid in _cola:
            _cola.remove(tid)
    if t:
        shutil.rmtree(Path(t["ruta"]).parent, ignore_errors=True)
    return {"borrado": bool(t)}

@app.post("/ocr/sincrono")
async def ocr_sincrono(archivo: UploadFile = File(...), paginas_max: int = Form(0)):
    """Espera el resultado. Comodo para probar con curl; la Gaceta usa /ocr."""
    r = await ocr(archivo, paginas_max)
    while True:
        t = _trabajos.get(r["id"])
        if t is None or t["estado"] in ("listo", "error"):
            break
        await asyncio.sleep(1)
    estado = ocr_estado(r["id"])
    ocr_borrar(r["id"])
    if estado["estado"] == "error":
        raise HTTPException(status_code=422, detail=estado["error"])
    return estado["resultado"]
