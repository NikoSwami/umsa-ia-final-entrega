# -*- coding: utf-8 -*-
"""
Cruza la BASE LEGAL de las unidades de MOF con las resoluciones del corpus y
mete esas aristas en el grafo de citas.

Es la mitad que hace que juntar los dos sistemas valga la pena: MOF sabe que
la unidad X fue creada por la resolucion HCF 393/2022, y la gaceta tiene el
texto de esa resolucion. Con la arista puesta, el motor puede ir en las dos
direcciones — de la unidad a su norma y de la norma a lo que creo — usando la
expansion del grafo que ya existe (motor_web, paso 6.5).

POR QUE ES UN PASO APARTE Y VA DESPUES
construir_adyacencia.py REESCRIBE grafo_citas.json entero cada vez
(construir_adyacencia.py:275). Si estas aristas se escribieran antes, la
siguiente reconstruccion se las llevaria por delante sin avisar. Por eso
enlazar_mof corre DESPUES en el pipeline y es idempotente: se puede repetir
sobre un grafo ya enlazado sin duplicar nada.

Las relaciones nuevas ('crea', 'sustenta') quedan FUERA de
GrafoCitas.RELS_VIGENCIA a proposito: crear una unidad no cambia la vigencia
de ninguna resolucion, y no deben competir con derogaciones y modificaciones
cuando alguien pregunta si una resolucion sigue vigente.
"""
import json
import os
from pathlib import Path

from . import exportador

try:
    from ..mejoras.referencia_directa import resolver
except (ImportError, ValueError):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mejoras"))
    from referencia_directa import resolver

DATA = Path(__file__).resolve().parents[1] / "data"
RAW = DATA / "raw"
GRAFO = DATA / "grafo_citas.json"

REL_CREACION = "crea"
REL_BASE_LEGAL = "sustenta"
MAX_POR_UNIDAD = 5

def fichas_mof(raw=None) -> list:
    """[(doc_id, ficha)] de las fichas de MOF presentes en data/raw/."""
    raw = Path(raw) if raw else RAW
    if not raw.is_dir():
        return []
    salida = []
    for ruta in sorted(raw.glob("doc_*.json")):
        if not exportador.es_ficha_mof(ruta.stem):
            continue
        try:
            with open(ruta, encoding="utf-8") as f:
                salida.append((ruta.stem, json.load(f)))
        except Exception:
            continue
    return salida

def referencias_de_ficha(ficha: dict) -> list:
    """[(org, num, anio)] de una ficha, del bloque 'mof' que dejo el
    exportador. Las fichas viejas sin ese bloque se re-parsean del texto."""
    bloque = (ficha or {}).get("mof") or {}
    refs = bloque.get("referencias")
    if isinstance(refs, list) and refs:
        salida = []
        for r in refs:
            if isinstance(r, (list, tuple)) and len(r) == 3:
                org = r[0] or None
                salida.append((org, str(r[1]), str(r[2])))
        return salida
    return []

def indice_identidad(grafo: dict) -> dict:
    """doc_id -> (org, num, anio) de las RESOLUCIONES del grafo.

    Las fichas de MOF se excluyen: no tienen numero propio y no deben poder
    ser el destino de una referencia legal.
    """
    ident = {}
    for did, info in (grafo.get("docs") or {}).items():
        if exportador.es_ficha_mof(did):
            continue
        ident[did] = (info.get("org"), info.get("num"), info.get("anio"))
    return ident

def referencia_creacion_de_ficha(ficha: dict):
    """La resolucion que CREO la unidad, o None si la unidad no declara una."""
    r = ((ficha or {}).get("mof") or {}).get("referencia_creacion")
    if isinstance(r, (list, tuple)) and len(r) == 3:
        return (r[0] or None, str(r[1]), str(r[2]))
    return None

def planificar(fichas, ident) -> dict:
    """Funcion PURA: doc_id de ficha -> {'cita': [...], 'rel': {...}}.

    No toca disco; es la parte que se puede probar sin corpus.

    'crea' se reserva para la resolucion que la unidad declara en
    `res_creacion`. Antes se daba a la primera referencia que resolviera, y eso
    convertia una base legal cualquiera en una afirmacion falsa: una unidad
    puede tener base legal y NO tener resolucion de creacion, y en ese caso
    ninguna de sus normas la creo.
    """
    plan = {}
    for doc_id, ficha in fichas:
        refs = referencias_de_ficha(ficha)
        if not refs:
            continue
        destinos = resolver(refs, ident, max_docs=MAX_POR_UNIDAD)
        if not destinos:
            continue

        creacion = referencia_creacion_de_ficha(ficha)
        doc_creacion = None
        if creacion:
            resuelto = resolver([creacion], ident, max_docs=1)
            doc_creacion = resuelto[0] if resuelto else None

        rel = {did: (REL_CREACION if did == doc_creacion else REL_BASE_LEGAL)
               for did in destinos}
        plan[doc_id] = {"cita": destinos, "rel": rel}
    return plan

def aplicar(grafo: dict, plan: dict) -> dict:
    """Mete el plan en el grafo (idempotente) y devuelve el resumen."""
    docs = grafo.setdefault("docs", {})
    aristas = 0
    for doc_id, datos in plan.items():
        nodo = docs.setdefault(doc_id, {"org": None, "num": None, "anio": None})
        citas = list(dict.fromkeys(list(nodo.get("cita") or []) + datos["cita"]))
        nodo["cita"] = citas
        rel = dict(nodo.get("rel") or {})
        rel.update(datos["rel"])
        nodo["rel"] = rel
        nodo["mof"] = True
        aristas += len(datos["cita"])
    meta = grafo.setdefault("meta", {})
    meta["enlaces_mof"] = {"unidades": len(plan), "aristas": aristas}
    return {"unidades_enlazadas": len(plan), "aristas": aristas}

def main(raw=None, grafo_ruta=None, verbose=True) -> dict:
    grafo_ruta = Path(grafo_ruta) if grafo_ruta else GRAFO
    if not grafo_ruta.exists():
        if verbose:
            print(f"  [enlaces MOF] no existe {grafo_ruta}: nada que enlazar.")
        return {"unidades_enlazadas": 0, "aristas": 0}

    fichas = fichas_mof(raw)
    if not fichas:
        if verbose:
            print("  [enlaces MOF] no hay fichas de MOF en data/raw/: no-op.")
        return {"unidades_enlazadas": 0, "aristas": 0}

    with open(grafo_ruta, encoding="utf-8") as f:
        grafo = json.load(f)

    plan = planificar(fichas, indice_identidad(grafo))
    resumen = aplicar(grafo, plan)

    tmp = grafo_ruta.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(grafo, f, ensure_ascii=False)
    os.replace(tmp, grafo_ruta)

    if verbose:
        sin_enlace = len(fichas) - resumen["unidades_enlazadas"]
        print(f"Fichas de MOF          : {len(fichas)}")
        print(f"Unidades enlazadas     : {resumen['unidades_enlazadas']}")
        print(f"Sin resolucion en corpus: {sin_enlace}")
        print(f"Aristas unidad->resolucion: {resumen['aristas']}")
        print(f"-> {grafo_ruta}")
    return resumen

if __name__ == "__main__":
    main()
