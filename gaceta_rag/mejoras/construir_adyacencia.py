# -*- coding: utf-8 -*-
"""
Convierte grafo_llm.json (hechos por-documento extraidos por Qwen) en la
ADYACENCIA que consume el motor: gaceta_rag/data/grafo_citas.json.

Es RAPIDO (sin LLM): resuelve cada numero de cita "NNN/AAAA" al doc_id real
usando un indice de numeros propios. Conserva el tipo de relacion (normalizado
a un vocabulario controlado).

Mejoras (2026-06):
  - NORMALIZACION de relaciones: el LLM emite etiquetas libres y con typos
    ('aprucba', 'aproba', 'abrogar', 'deja sin efecto', ...). Se mapean a un
    vocabulario controlado (aprueba/modifica/deroga/complementa/cumple/...).
  - RECUPERACION de citas ambiguas (por defecto ON, --no-recuperar para
    apagar): cuando un numero apunta a varios doc_id (numeros propios
    duplicados por errores de extraccion) se elige el mejor candidato
    (mismo organo + doc previo mas cercano) en vez de descartarlo.
  - RECUPERACION de citas EXTERNAS (por defecto ON, --no-externas para
    apagar): cuando el numero de una cita no esta en el indice del LLM, se
    busca en un INDICE SECUNDARIO construido del encabezado del CONTENIDO
    ("CONSEJO FACULTATIVO No.NNN/AAAA") y del TITULO de los raw docs, que es
    mas fiable para el AÑO que la extraccion del LLM. Asi se rescatan citas
    a resoluciones que SI estan en el corpus pero cuyo numero propio el LLM
    no habia indexado. Las externas que aun no matchean son resoluciones
    realmente fuera del corpus (reglamentos viejos, otras facultades).

  Las aristas inferidas (ambiguas u externas recuperadas) se marcan en
  'cita_inf' para poder auditarlas o filtrarlas.

Salida: gaceta_rag/data/grafo_citas.json
    {"meta": {...}, "docs": {"doc_00001": {"org","num","anio","cita":[id...],
              "rel":{id:relacion}, "cita_inf":[ids inferidos]}}}
"""
import json
import argparse
import datetime
import glob
import os
import re
from pathlib import Path
from collections import defaultdict, Counter

try:
    from .identidad import FACULTAD_DEFECTO, facultad_de_raw
except ImportError:
    from identidad import FACULTAD_DEFECTO, facultad_de_raw

DATA = Path(__file__).resolve().parents[1] / "data"
LLM = DATA / "grafo_llm.json"
OUT = DATA / "grafo_citas.json"
RAW = DATA / "raw"

def norm(numero):
    """'042/2021' -> ('42','2021'); tolera None/basura -> None."""
    if not numero or "/" not in str(numero):
        return None
    a, b = str(numero).split("/", 1)
    a = "".join(ch for ch in a if ch.isdigit())
    b = "".join(ch for ch in b if ch.isdigit())
    if not a or len(b) != 4:
        return None
    return (str(int(a)), b)

def doc_n(did):
    """'doc_00044' -> 44 (para comparar cercania/orden cronologico aprox)."""
    d = "".join(ch for ch in str(did) if ch.isdigit())
    return int(d) if d else 0

_REL_REGLAS = [
    ("deja sin efecto", "deroga"), ("abrog", "deroga"), ("derog", "deroga"),
    ("modific", "modifica"),
    ("homolog", "homologa"),
    ("complement", "complementa"),
    ("subsan", "subsana"),
    ("cumpl", "cumple"),
    ("aprob", "aprueba"), ("aprueb", "aprueba"), ("aprucb", "aprueba"),
    ("autoriz", "autoriza"),
    ("conform", "conforma"),
    ("aval", "avala"),
    ("design", "designa"),
    ("convoc", "convoca"),
    ("dispon", "dispone"),
    ("refrend", "referencia"), ("refer", "referencia"), ("refere", "referencia"),
    ("visto", "referencia"), ("considera", "referencia"), ("cita", "referencia"),
    ("solicit", "referencia"), ("suscri", "referencia"), ("envia", "referencia"),
    ("sugiere", "referencia"), ("conformidad", "referencia"), ("acompa", "referencia"),
]
RELACIONES_VALIDAS = sorted({c for _, c in _REL_REGLAS})

def canon_rel(r):
    """Normaliza una relacion libre del LLM al vocabulario controlado."""
    if not r:
        return "referencia"
    s = str(r).strip().lower()
    if not s or s == "null":
        return "referencia"
    for sub, canon in _REL_REGLAS:
        if sub in s:
            return canon
    return "referencia"

def elegir_candidato(cands, citer, ident, fac=None):
    """Desambigua una cita que apunta a varios doc_id: prefiere la misma
    FACULTAD que el citante (si `fac` la conoce; una resolucion cita casi
    siempre dentro de su facultad), luego el mismo organo y, entre esos, el
    documento PREVIO mas cercano (las resoluciones suelen citar otras
    anteriores)."""
    if fac:
        misma_fac = [c for c in cands if fac.get(c) == fac.get(citer)]
        if misma_fac:
            cands = misma_fac
    citer_org = ident.get(citer, (None,))[0]
    citn = doc_n(citer)
    mismos = [c for c in cands if ident.get(c, (None,))[0] == citer_org] or cands
    previos = [c for c in mismos if doc_n(c) < citn]
    if previos:
        return max(previos, key=doc_n)
    return min(mismos, key=lambda c: abs(doc_n(c) - citn))

_HDR = re.compile(r"No\.?\s*(\d{1,4})\s*[/-]\s*(20\d{2})")
_TIT = re.compile(r"(?:HCF|HCU|HCC|HCD|RHCF|RR)[\s/_.-]*(\d{1,4})[\s/_.-]+(20\d{2})", re.I)

def numero_de_raw(doc):
    """(num,anio) desde el encabezado del contenido o el titulo del raw doc."""
    m = _HDR.search(str(doc.get("contenido") or "")[:250])
    if m:
        return (str(int(m.group(1))), m.group(2))
    m = _TIT.search(str(doc.get("titulo") or ""))
    if m:
        return (str(int(m.group(1))), m.group(2))
    return None

def cargar_indice_secundario():
    """Una pasada por los raw docs -> (sec, fac):
    sec : (num,anio) -> [doc_id]  (contenido/titulo del corpus).
    fac : doc_id -> facultad  (campo 'Facultad' de Paperless o el default)."""
    sec, fac = defaultdict(list), {}
    if not RAW.exists():
        return sec, fac
    for f in glob.glob(str(RAW / "doc_*.json")):
        did = os.path.splitext(os.path.basename(f))[0]
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        fac[did] = facultad_de_raw(d)
        key = numero_de_raw(d)
        if key:
            sec[key].append(did)
    return sec, fac

def main(recuperar=True, recuperar_externas=True):
    if not LLM.exists():
        print(f"No existe {LLM}. Corre primero extraer_grafo_llm.py")
        return
    data = json.load(open(LLM, encoding="utf-8"))
    docs = data.get("docs", {})

    sec, fac_raw = cargar_indice_secundario()
    if recuperar_externas:
        print(f"Indice secundario (raw): {sum(len(v) for v in sec.values())} docs, "
              f"{len(sec)} claves")
    else:
        sec = {}

    idx = defaultdict(list)
    ident = {}
    fac = dict(fac_raw)
    for did, info in docs.items():
        if "_error" in info:
            continue
        key = norm(info.get("numero"))
        ident[did] = (info.get("organo"), key[0] if key else None, key[1] if key else None)
        fac[did] = info.get("facultad") or fac_raw.get(did) or FACULTAD_DEFECTO
        if key:
            idx[key].append(did)

    out_docs = {}
    n_edges = n_amb = n_amb_rec = n_ext = n_ext_rec = 0
    rel_counter = Counter()
    for did, info in docs.items():
        if "_error" in info:
            continue
        org, num, anio = ident.get(did, (None, None, None))
        cita_ids, rel, inferidas = [], {}, []
        for c in (info.get("cita") or []):
            if not isinstance(c, dict):
                continue
            key = norm(c.get("numero"))
            if not key:
                continue
            cands = [x for x in idx.get(key, []) if x != did]
            tgt, inferida = None, False
            if len(cands) == 1:
                tgt = cands[0]
            elif len(cands) > 1:
                if recuperar:
                    tgt = elegir_candidato(cands, did, ident, fac=fac)
                    inferida = True
                    n_amb_rec += 1
                else:
                    n_amb += 1
            else:
                cands2 = [x for x in sec.get(key, []) if x != did]
                if recuperar_externas and cands2:
                    tgt = cands2[0] if len(cands2) == 1 else elegir_candidato(cands2, did, ident, fac=fac)
                    inferida = True
                    n_ext_rec += 1
                else:
                    n_ext += 1
            if tgt and tgt not in cita_ids:
                cita_ids.append(tgt)
                rel[tgt] = canon_rel(c.get("relacion"))
                rel_counter[rel[tgt]] += 1
                n_edges += 1
                if inferida:
                    inferidas.append(tgt)
        entry = {"org": org, "num": num, "anio": anio}
        if fac.get(did, FACULTAD_DEFECTO) != FACULTAD_DEFECTO:
            entry["fac"] = fac[did]
        if cita_ids:
            entry["cita"] = cita_ids
            entry["rel"] = rel
            if inferidas:
                entry["cita_inf"] = inferidas
        out_docs[did] = entry

    salida = {
        "meta": {
            "fuente": "grafo_llm.json (Qwen) + indice raw (contenido/titulo)",
            "docs": len(out_docs),
            "citaciones": n_edges,
            "citaciones_ambiguas_recuperadas": n_amb_rec,
            "citaciones_externas_recuperadas": n_ext_rec,
            "citas_ambiguas_descartadas": n_amb,
            "citas_externas_restantes": n_ext,
            "relaciones": dict(rel_counter),
            "facultades": dict(Counter(fac.get(d, FACULTAD_DEFECTO) for d in out_docs)),
            "recuperar_ambiguas": recuperar,
            "recuperar_externas": recuperar_externas,
            "generado": datetime.date.today().isoformat(),
        },
        "docs": out_docs,
    }
    if OUT.exists():
        import shutil
        bak = OUT.with_suffix(".json.bak")
        shutil.copy2(OUT, bak)
        print(f"(respaldo del grafo anterior -> {bak.name})")

    json.dump(salida, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    con_cita = sum(1 for d in out_docs.values() if d.get("cita"))
    print(f"Docs                     : {len(out_docs)}")
    print(f"Cobertura (con cita)     : {con_cita}  ({100 * con_cita / max(1, len(out_docs)):.1f}%)")
    print(f"Citaciones (aristas)     : {n_edges}")
    print(f"  ambiguas recuperadas   : {n_amb_rec}")
    print(f"  externas recuperadas   : {n_ext_rec}")
    print(f"  externas restantes     : {n_ext}  (fuera del corpus)")
    print(f"Relaciones               : {dict(rel_counter.most_common(12))}")
    print(f"-> {OUT}")

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Construye la adyacencia del grafo de citas.")
    p.add_argument("--no-recuperar", action="store_true",
                   help="No recuperar citas ambiguas.")
    p.add_argument("--no-externas", action="store_true",
                   help="No recuperar citas externas via indice raw.")
    args = p.parse_args()
    main(recuperar=not args.no_recuperar, recuperar_externas=not args.no_externas)
