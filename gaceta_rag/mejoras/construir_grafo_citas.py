# -*- coding: utf-8 -*-
"""
Construye el GRAFO DE CITACIONES de runtime para GraphRAG.

A diferencia del grafo visual de graphify/, este se keyea por el MISMO doc_id
que usa el motor en tiempo real (el stem del archivo: doc_00001), para poder
engancharlo a la recuperacion en motor_web.py.

Cada documento -> (organo, num, anio) extraidos del titulo/contenido.
Citacion doc_A -> doc_B cuando el texto de A referencia el numero de B, con
matching robusto:
  - el numero propio (cabecera) NO cuenta como citacion,
  - si la referencia trae organo explicito -> match (organo, num, anio),
  - si no, solo se enlaza cuando (num, anio) apunta a UN unico documento
    (evita colisiones num/anio entre organos).

Salida: gaceta_rag/data/grafo_citas.json
    {"meta": {...}, "docs": {"doc_00001": {"org","num","anio","cita":[...]}, ...}}

Ejecutar:
    venv\\Scripts\\python.exe gaceta_rag\\mejoras\\construir_grafo_citas.py
"""
import json
import glob
import re
import os
import argparse
import datetime

AQUI = os.path.dirname(__file__)
RAW = os.path.normpath(os.path.join(AQUI, "..", "data", "raw"))
OUT = os.path.normpath(os.path.join(AQUI, "..", "data", "grafo_citas.json"))

ORGANOS = ["RHCF", "RHCU", "HCF", "HCU", "HCC", "HCD"]
ORG_RE = "|".join(sorted(ORGANOS, key=len, reverse=True))
TIT_RE = re.compile(r"\b(" + ORG_RE + r")\b[\s_Nº°:.-]*?(\d{1,4})[\s_/.-]+(\d{4})", re.I)

NUM_RE = re.compile(r"N[°ºo\.\?\*º°�\s]{0,4}(\d{1,4})\s*/\s*(\d{4})")
HEADER_CHARS = 350

RES_MARK = re.compile(
    r"(RESOLUC|R\.?H\.?C|\bHC[FUC]\b|CONSEJO\s+(FACULTATIVO|UNIVERSITARIO)|"
    r"CONSEJO\s+DE\s+CARRERA)", re.I)
NOTE_MARK = re.compile(
    r"(INFORME|\bINF\b|\bNOTA\b|\bCITE\b|STRIA|\bDIR\b|FCPN\.|MEMOR|"
    r"DECRETO|\bLEY\b|CONVENIO|CONVOCATORIA|FORMULARIO|PROGRAMA|"
    r"DICTAMEN|TRIBUNAL|\bHOJA\b|\bACTA\b)", re.I)

def organo_por_cuerpo(txt: str):
    t = txt.upper()
    if "CONSEJO FACULTATIVO" in t:     return "HCF"
    if "CONSEJO UNIVERSITARIO" in t:   return "HCU"
    if "CONSEJO DE CARRERA" in t:      return "HCC"
    if "CONSEJO DE DEPARTAMENTO" in t: return "HCD"
    return None

def identidad(titulo: str, contenido: str, etiquetas):
    """Devuelve (organo, num, anio) del documento, o (org, None, None).

    El numero PROPIO se busca primero en la cabecera (primeros HEADER_CHARS),
    que es donde va "RESOLUCION N° NNN/AAAA". Asi se evita confundirlo con
    las resoluciones referenciadas en el VISTOS, que aparecen mas abajo.
    """
    organo = num = anio = None
    m = TIT_RE.search(titulo or "")
    if m:
        organo, num, anio = m.group(1).upper(), m.group(2), m.group(3)
    if not organo and etiquetas:
        organo = str(etiquetas[0]).upper()
    if not organo:
        organo = organo_por_cuerpo(contenido or "")
    if not (num and anio):
        c = contenido or ""
        mc = NUM_RE.search(c[:HEADER_CHARS]) or NUM_RE.search(c)
        if mc:
            num, anio = mc.group(1), mc.group(2)
    return organo, num, anio

def main(force=False):
    if not force and os.path.exists(OUT):
        try:
            prev = json.load(open(OUT, encoding="utf-8"))
            fuente = str(prev.get("meta", {}).get("fuente", ""))
            if "qwen" in fuente.lower() or "llm" in fuente.lower():
                aristas = prev.get("meta", {}).get("citaciones", "?")
                print(f"  [ABORTADO] {os.path.basename(OUT)} ya es el grafo Qwen "
                      f"({aristas} aristas; fuente: {fuente}).")
                print("  Este script (regex) lo DEGRADARIA. Usa --force para sobrescribir igual.")
                return
        except Exception:
            pass

    files = sorted(glob.glob(os.path.join(RAW, "*.json")))
    docs = {}
    idx_org = {}
    idx_na = {}

    for f in files:
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        doc_id = os.path.splitext(os.path.basename(f))[0]
        contenido = d.get("contenido") or ""
        organo, num, anio = identidad(d.get("titulo") or "", contenido, d.get("etiquetas") or [])
        docs[doc_id] = {"org": organo, "num": num, "anio": anio, "txt": contenido}
        if num and anio:
            idx_na.setdefault((num, anio), []).append(doc_id)
            if organo:
                idx_org.setdefault((organo, num, anio), []).append(doc_id)

    n_edges = 0
    for doc_id, info in docs.items():
        propia = (info["num"], info["anio"])
        txt = info["txt"]
        cita = set()
        for m in NUM_RE.finditer(txt):
            key = (m.group(1), m.group(2))
            if key == propia:
                continue
            ini = max(0, m.start() - 50)
            ventana = txt[ini:m.start()]
            if not RES_MARK.search(ventana) or NOTE_MARK.search(ventana):
                continue
            ventana = ventana.upper()
            org_ref = None
            mo = re.search(ORG_RE, ventana)
            if mo:
                org_ref = mo.group(0).upper()
            elif "FACULTATIVO" in ventana:    org_ref = "HCF"
            elif "UNIVERSITARIO" in ventana:  org_ref = "HCU"
            elif "CARRERA" in ventana:        org_ref = "HCC"

            destino = None
            if org_ref and (org_ref, key[0], key[1]) in idx_org:
                cands = idx_org[(org_ref, key[0], key[1])]
                if len(cands) == 1:
                    destino = cands[0]
            if destino is None:
                cands = idx_na.get(key, [])
                if len(cands) == 1:
                    destino = cands[0]
            if destino and destino != doc_id:
                cita.add(destino)
        info["cita"] = sorted(cita)
        n_edges += len(cita)

    out_docs = {
        doc_id: {"org": i["org"], "num": i["num"], "anio": i["anio"], "cita": i["cita"]}
        for doc_id, i in docs.items()
        if i["cita"] or (i["num"] and i["anio"])
    }
    con_id = sum(1 for i in docs.values() if i["num"] and i["anio"])
    salida = {
        "meta": {
            "docs_total": len(docs),
            "docs_con_identidad": con_id,
            "docs_exportados": len(out_docs),
            "citaciones": n_edges,
            "generado": datetime.date.today().isoformat(),
        },
        "docs": out_docs,
    }
    json.dump(salida, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)

    print(f"Documentos totales     : {len(docs)}")
    print(f"Con identidad num/anio : {con_id}")
    print(f"Citaciones doc->doc    : {n_edges}")
    print(f"Documentos exportados  : {len(out_docs)}")
    print(f"-> {OUT}")

if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Construye el grafo de citas REGEX (fallback sin LLM).")
    p.add_argument("--force", action="store_true",
                   help="Sobrescribir aunque exista un grafo Qwen mas rico.")
    main(force=p.parse_args().force)
