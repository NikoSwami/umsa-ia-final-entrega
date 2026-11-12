# -*- coding: utf-8 -*-
"""
Aplica la identidad determinista (numero/organo desde el TITULO, ver
identidad.py) a un grafo_llm.json YA EXTRAIDO, sin re-escanear con el LLM.

Por que existe: la correccion de identidad se añadio a extraer_grafo_llm.py
DESPUES de que el grafo commiteado fuera extraido, asi que los ~3.234 docs
del JSON traen el numero/organo crudo del LLM (con errores de OCR tipo
'21059' por '1059'). Este script corrige el JSON existente en segundos y
sin GPU. Es IDEMPOTENTE (correrlo dos veces no cambia nada) y NO
INTERACTIVO (apto para pipelines automaticos: lo invoca construir_todo.py
antes de construir_adyacencia.py).

Uso:
    python corregir_identidad_grafo.py            # corrige in-place (con .bak)
    python corregir_identidad_grafo.py --dry-run  # solo reporta, no escribe
"""
import argparse
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE.parent / "data"
RAW = DATA / "raw"
GRAFO_LLM = DATA / "grafo_llm.json"

sys.path.insert(0, str(BASE))
from identidad import aplicar_identidad

def corregir_docs(docs, cargar_raw):
    """Corrige numero/organo de cada entrada usando el titulo del raw doc.

    docs       : dict doc_id -> entrada del LLM (se muta).
    cargar_raw : callable doc_id -> dict raw ({'titulo','contenido'}) o None.
    Devuelve (n_numero, n_organo, n_sin_raw): cuantos numeros y organos
    cambiaron y cuantos docs no tienen raw correspondiente.
    """
    n_num = n_org = n_sin = 0
    for doc_id, entrada in docs.items():
        raw = cargar_raw(doc_id)
        if raw is None:
            n_sin += 1
            continue
        antes_num = entrada.get("numero")
        antes_org = entrada.get("organo")
        aplicar_identidad(raw, entrada)
        if entrada.get("numero") != antes_num:
            n_num += 1
        if entrada.get("organo") != antes_org:
            n_org += 1
    return n_num, n_org, n_sin

def _cargar_raw_disco(doc_id):
    ruta = RAW / f"{doc_id}.json"
    if not ruta.exists():
        return None
    with open(ruta, encoding="utf-8") as f:
        return json.load(f)

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--dry-run", action="store_true",
                    help="reportar cambios sin escribir el JSON")
    args = ap.parse_args(argv)

    if not GRAFO_LLM.exists():
        print(f"No existe {GRAFO_LLM}; nada que corregir.")
        return 0

    with open(GRAFO_LLM, encoding="utf-8") as f:
        grafo = json.load(f)
    docs = grafo.get("docs", {})

    n_num, n_org, n_sin = corregir_docs(docs, _cargar_raw_disco)
    print(f"Docs: {len(docs)} | numeros corregidos: {n_num} | "
          f"organos corregidos: {n_org} | sin raw: {n_sin}")

    if args.dry_run:
        print("(dry-run: no se escribio nada)")
        return 0
    if n_num == 0 and n_org == 0:
        print("Sin cambios: el grafo ya tiene la identidad del titulo.")
        return 0

    bak = str(GRAFO_LLM) + ".bak"
    if not os.path.exists(bak):
        os.replace(GRAFO_LLM, bak)
        origen = bak
    else:
        origen = str(GRAFO_LLM)
    tmp = str(GRAFO_LLM) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(grafo, f, ensure_ascii=False)
    os.replace(tmp, GRAFO_LLM)
    print(f"Guardado {GRAFO_LLM} (backup previo: {origen})")
    print("Ahora corre construir_adyacencia.py para regenerar grafo_citas.json.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
