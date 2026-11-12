# -*- coding: utf-8 -*-
"""
Arranque del contenedor de la Gaceta en MODO API.

Hace lo mismo que las celdas de colab/Ejecutar_App_Colab1.ipynb, en orden y
sin preguntar nada (la configuracion viene del .env):

  1. el cerebro: GACETA_BACKEND=api, la clave y el modelo (si no se fijo uno,
     se elige UNA vez contra el catalogo vivo, como la celda 2);
  2. los modelos de la busqueda: e5 y el reranker desde modelos_mejoras/ (la
     carpeta del proyecto, montada); si faltan, se bajan una vez ahi mismo
     (celda 3);
  3. la base vectorial: tiene que estar en el volumen de datos (celda 4);
  4. el token de la carga manual (y de la del respaldo de MOF, /carga-mof): si
     no hay ninguno, se genera uno, se guarda en el volumen y se muestra aqui;
  5. api_gaceta.py escuchando en 0.0.0.0 (celda 6). Reemplaza a este proceso
     (exec), asi recibe directo las señales de docker stop.

Nada de esto tumba el contenedor: si falta la clave, la app arranca igual
(la carga de documentos funciona) y el chat explica que falta.
"""
import os
import secrets
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
DATA = RAIZ / "gaceta_rag" / "data"
MODELOS = RAIZ / "modelos_mejoras"
sys.path.insert(0, str(RAIZ))

E5 = ("multilingual-e5-base", "intfloat/multilingual-e5-base", "~1.1 GB")
RERANKER = ("bge-reranker-v2-m3", "BAAI/bge-reranker-v2-m3", "~2.2 GB")

def decir(msg=""):
    print(f"[arranque] {msg}", flush=True)

def recuadro(*lineas):
    print("=" * 72, flush=True)
    for l in lineas:
        print(f"  {l}", flush=True)
    print("=" * 72, flush=True)

def _apagado(nombre):
    return (os.environ.get(nombre) or "").strip().lower() in ("0", "false", "no")

def configurar_backend():
    os.environ.setdefault("GACETA_BACKEND", "api")
    backend = os.environ["GACETA_BACKEND"].strip().lower()
    if backend in ("", "qwen", "local"):
        recuadro("GACETA_BACKEND=qwen no esta soportado en este contenedor:",
                 "la imagen no trae llama-cpp ni GPU. Se usa GACETA_BACKEND=api.")
        os.environ["GACETA_BACKEND"] = "api"
    import motor_api

    proveedor = (os.environ.get("GACETA_API_PROVEEDOR")
                 or (backend if backend in motor_api.PROVEEDORES else "gemini"))
    os.environ["GACETA_API_PROVEEDOR"] = proveedor
    try:
        motor_api.api_key(proveedor)
    except RuntimeError as exc:
        cfg = motor_api.PROVEEDORES.get(proveedor, {})
        recuadro("FALTA LA CLAVE DE LA API: el chat no va a poder responder.",
                 str(exc),
                 f"Pone {cfg.get('claves', ('GACETA_API_KEY',))[0]}=... en el .env y",
                 "reinicia:  docker compose up -d",
                 "La carga de documentos funciona igual sin la clave.")
        return
    if (os.environ.get("GACETA_API_MODELO") or "").strip():
        decir(f"Cerebro: {proveedor} / {os.environ['GACETA_API_MODELO']} (fijado en el .env)")
        return
    decir(f"Eligiendo un modelo de {proveedor} que responda...")
    try:
        modelo = motor_api.resolver_modelo(proveedor, estricto=False)
    except Exception as exc:
        modelo = motor_api.PROVEEDORES[proveedor]["modelo"]
        decir(f"no se pudo consultar el catalogo ({motor_api.resumen_error(str(exc))}); "
              f"uso el de por defecto")
    os.environ["GACETA_API_MODELO"] = modelo
    decir(f"Cerebro: {proveedor} / {modelo}")

def _traer(carpeta, repo, tam) -> bool:
    destino = MODELOS / carpeta
    if (destino / "config.json").exists():
        decir(f"{carpeta}: en modelos_mejoras/ (no se baja nada)")
        return True
    if _apagado("DESCARGAR_MODELOS"):
        decir(f"{carpeta}: no esta y DESCARGAR_MODELOS=0")
        return False
    decir(f"{carpeta}: no esta en modelos_mejoras/ -> bajando de HuggingFace "
          f"({tam}, solo esta vez)...")
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(repo, local_dir=str(destino))
        return (destino / "config.json").exists()
    except Exception as exc:
        decir(f"{carpeta}: no se pudo bajar ({exc})")
        return False

def configurar_modelos():
    MODELOS.mkdir(parents=True, exist_ok=True)
    e5_ok = _traer(*E5)
    if e5_ok:
        os.environ["GACETA_MODELO_EMBEDDINGS"] = str(MODELOS / E5[0])
    else:
        decir("e5 local no disponible: el motor usara el id de "
              "embedding_config_v2.json (y lo bajara al cache si puede)")
    rr_ok = True
    if not _apagado("USAR_RERANKER"):
        rr_ok = _traer(*RERANKER)
        if not rr_ok:
            os.environ["USAR_RERANKER"] = "0"
            decir("reranker apagado: la app funciona con el orden de la "
                  "busqueda hibrida")
    else:
        decir("reranker apagado (USAR_RERANKER=0)")
    if e5_ok and rr_ok:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

def revisar_base():
    faltan = [n for n in ("faiss_index_v2.bin", "faiss_metadata_v2.json",
                          "chunks_v2.sqlite", "embedding_config_v2.json")
              if not (DATA / n).exists()]
    if faltan:
        recuadro("El volumen de datos no tiene la base vectorial:",
                 ", ".join(faltan),
                 "Se llena sola desde la imagen la PRIMERA vez que se crea el",
                 "volumen. Si lo creaste vacio a mano:",
                 "  docker compose down -v   (borra el volumen)  y  docker compose up -d")
        return
    from gaceta_rag.carga.ids import es_carga_manual
    docs = [p.stem for p in (DATA / "raw").glob("doc_*.json")]
    n_raw = len(docs)
    n_manual = sum(1 for d in docs if es_carga_manual(d))
    decir(f"Base: {n_raw} documentos en raw/ ({n_manual} cargados a mano)")

def configurar_token():
    if os.environ.get("GACETA_CARGA_TOKEN") or os.environ.get("GACETA_ADMIN_TOKEN"):
        decir("Token de carga: el del .env")
        return
    ruta = DATA / "cargas" / "token_carga.txt"
    try:
        token = ruta.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if not token:
        token = secrets.token_urlsafe(24)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(token + "\n", encoding="utf-8")
    os.environ["GACETA_CARGA_TOKEN"] = token
    recuadro("Token para la pagina de carga (http://127.0.0.1:8000/carga)",
             "y la del respaldo de MOF (http://127.0.0.1:8000/carga-mof):",
             token,
             "Se genero solo y queda guardado en el volumen de datos. Para fijar",
             "uno propio, pone GACETA_CARGA_TOKEN=... en el .env.")

def main():
    decir("Gaceta IA en Docker (modo API)")
    configurar_backend()
    configurar_modelos()
    revisar_base()
    configurar_token()
    ocr = os.environ.get("OCR_URL")
    decir(f"OCR: {ocr}" if ocr else "OCR: sin configurar (solo PDF con texto)")
    if not os.environ.get("MOF_URL"):
        decir("MOF: sin configurar (el chat funciona sin organigrama en vivo)")
    puerto = os.environ.get("GACETA_PUERTO_INTERNO", "8000")
    args = [sys.executable, str(RAIZ / "api_gaceta.py"), "--host", "0.0.0.0",
            "--port", puerto, "--no-browser"]
    os.chdir(RAIZ)
    if os.name == "nt":
        import subprocess
        sys.exit(subprocess.call(args))
    os.execv(sys.executable, args)

if __name__ == "__main__":
    main()
