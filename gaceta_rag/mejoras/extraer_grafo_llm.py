# -*- coding: utf-8 -*-
"""
Extractor del grafo de citas con el LLM LOCAL (Qwen2.5-7B) — INCREMENTAL.

Disenado para construir el grafo poco a poco, SIN saturar la maquina y SIN
reprocesar lo ya hecho:

  * INCREMENTAL : solo procesa documentos nuevos o cambiados (hash de contenido).
                  Al agregar un documento nuevo, vuelve a correr y solo extrae ese.
  * RESUMIBLE   : guarda el avance cada N documentos de forma atomica. Si se
                  corta (Ctrl+C, apagon), al volver a correr continua donde quedo.
  * SUAVE (throttle): pausa configurable entre documentos para no tener la GPU
                  al 100%. Mas lento, pero la PC sigue usable y mas fresca.
  * ROBUSTO     : fuerza salida JSON valida (response_format json_object) y
                  tolera errores por documento sin abortar el lote.

Salida: gaceta_rag/data/grafo_llm.json
  {"meta": {...}, "docs": {"doc_00001": {"numero","organo","tema","cita":[{numero,relacion}],"personas":[...],"_hash"}}}

Uso:
  venv\\Scripts\\python.exe gaceta_rag\\mejoras\\extraer_grafo_llm.py            # procesa TODO lo pendiente
  ...extraer_grafo_llm.py --limite 200                # solo 200 (para ir por tandas)
  ...extraer_grafo_llm.py --doc doc_03240             # un documento concreto (recien agregado)
  ...extraer_grafo_llm.py --pausa 2.0                 # 2 s entre docs (mas suave)
  ...extraer_grafo_llm.py --reprocesar                # ignora el cache y rehace todo
"""
import os
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import sys
import json
import glob
import time
import hashlib
import argparse
import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "gaceta_rag"))

import motor_qwen as base
import identidad

DATA = RAIZ / "gaceta_rag" / "data"
RAW = DATA / "raw"
OUT = DATA / "grafo_llm.json"

MAX_CHARS_DEF = 3000

def doc_hash(contenido: str) -> str:
    """Hash del contenido COMPLETO (no solo lo que se le manda al LLM): es
    para detectar si el doc cambio entre corridas incrementales, y un
    cambio mas alla de MAX_CHARS_DEF tambien cuenta."""
    return hashlib.sha1(contenido.encode("utf-8", "ignore")).hexdigest()[:12]

def cargar_grafo() -> dict:
    if OUT.exists():
        try:
            return json.load(open(OUT, encoding="utf-8"))
        except Exception:
            print("  [WARN] grafo_llm.json corrupto; empiezo de cero")
    return {"meta": {}, "docs": {}}

def guardar_atomico(grafo: dict):
    """Escribe a un .tmp y renombra: nunca deja el JSON a medias."""
    grafo["meta"]["docs"] = len(grafo["docs"])
    grafo["meta"]["actualizado"] = datetime.datetime.now().isoformat(timespec="seconds")
    tmp = OUT.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(grafo, f, ensure_ascii=False)
    os.replace(tmp, OUT)

def extraer_uno(llm, titulo, contenido: str, max_chars: int):
    try:
        r = llm.create_chat_completion(
            messages=identidad.construir_mensajes(titulo, contenido, max_chars),
            max_tokens=380,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        return json.loads(r["choices"][0]["message"]["content"])
    except Exception as e:
        return {"_error": str(e)[:120]}

def main():
    ap = argparse.ArgumentParser(description="Extractor incremental del grafo de citas con Qwen")
    ap.add_argument("--limite", type=int, default=0, help="maximo de docs a procesar (0 = todos los pendientes)")
    ap.add_argument("--doc", default=None, help="procesar un doc_id concreto (ej. doc_03240)")
    ap.add_argument("--pausa", type=float, default=1.0, help="segundos de pausa entre docs (throttle)")
    ap.add_argument("--max-chars", type=int, default=MAX_CHARS_DEF, help="chars del doc a leer")
    ap.add_argument("--guardar-cada", type=int, default=10, help="guardar avance cada N docs")
    ap.add_argument("--reprocesar", action="store_true", help="ignora el cache y rehace todo")
    ap.add_argument("--solo-faltantes", action="store_true",
                    help="procesar SOLO los docs que no estan en el grafo; no "
                         "re-extraer los que si estan aunque su texto haya cambiado")
    args = ap.parse_args()

    grafo = cargar_grafo()
    hechos = grafo["docs"]

    if args.doc:
        files = [RAW / f"{args.doc}.json"]
    else:
        files = [Path(p) for p in sorted(glob.glob(str(RAW / "*.json")))]

    pendientes = []
    desactualizados = 0
    for f in files:
        if not f.exists():
            print(f"  [WARN] no existe {f.name}")
            continue
        did = f.stem
        try:
            cont = (json.load(open(f, encoding="utf-8")).get("contenido") or "")
        except Exception:
            continue
        if not cont.strip():
            continue
        h = doc_hash(cont)
        en_grafo = did in hechos
        al_dia   = en_grafo and hechos[did].get("_hash") == h
        if (not args.reprocesar) and al_dia:
            continue
        if en_grafo and not al_dia:
            desactualizados += 1
            if args.solo_faltantes:
                continue
        pendientes.append((did, f, h))

    if args.limite:
        pendientes = pendientes[:args.limite]

    total = len(pendientes)
    print(f"Documentos ya en el grafo : {len(hechos)}")
    if desactualizados:
        nota = "  (OMITIDOS por --solo-faltantes)" if args.solo_faltantes else ""
        print(f"Con el texto ya cambiado  : {desactualizados}{nota}")
    print(f"Pendientes a procesar     : {total}")
    if total == 0:
        print("Nada que hacer. El grafo esta al dia.")
        return

    print("Cargando Qwen2.5-7B (local)...")
    llm = base.cargar_modelo()
    print(f"Throttle: pausa de {args.pausa}s entre documentos.\n")

    t0 = time.time()
    n_ok = n_err = 0
    try:
        for i, (did, f, h) in enumerate(pendientes, 1):
            doc = json.load(open(f, encoding="utf-8"))
            cont = doc.get("contenido") or ""
            j = extraer_uno(llm, doc.get("titulo"), cont, args.max_chars)
            if "_error" in j:
                n_err += 1
            else:
                identidad.aplicar_identidad(doc, j)
                n_ok += 1
            j["_hash"] = h
            hechos[did] = j
            if i % args.guardar_cada == 0:
                guardar_atomico(grafo)
                vel = (time.time() - t0) / i
                rest = vel * (total - i)
                print(f"  {i}/{total}  ({n_ok} ok, {n_err} err)  "
                      f"{vel:.1f}s/doc  ETA {rest/60:.0f} min")
            if args.pausa > 0:
                time.sleep(args.pausa)
    except KeyboardInterrupt:
        print("\n  Interrumpido: guardando avance...")
    finally:
        guardar_atomico(grafo)

    dt = time.time() - t0
    print(f"\nListo. Procesados {n_ok + n_err} ({n_ok} ok, {n_err} err) en {dt/60:.1f} min.")
    print(f"Grafo guardado: {OUT}  (total {len(hechos)} docs)")
    print("Siguiente paso (rapido, sin LLM): construir la adyacencia para el motor:")
    print("  venv\\Scripts\\python.exe gaceta_rag\\mejoras\\construir_adyacencia.py")

if __name__ == "__main__":
    main()
