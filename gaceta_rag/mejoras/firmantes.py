# -*- coding: utf-8 -*-
"""
Deteccion de FIRMANTES/TRAMITADORES habituales: separa las resoluciones que
son "de" una persona de las que esa persona solo FIRMO o TRAMITO como
autoridad.

Por que existe: grafo_llm.json trae, por doc, TODAS las "personas" que el
LLM vio en el texto — incluido el decano/secretario que firma al pie, el
boilerplate de delegacion de firma del CONSIDERANDO ("...AUTORIZA al M.Sc.
Fulano, decano en ejercicio de la Facultad..., la emision y firma de
resoluciones...") y el director de carrera/instituto que ENVIA la nota que
la resolucion tramita ("VISTOS: la nota FCPN.INF.DIR N 1084/2022 enviada
por el Ph.D. Fulano, director de la carrera de informatica,
solicitando..."). Resultado: el decano quedaba indexado en ~214 docs, el
ex-director de informatica en 536 (SIN una sola firma al pie: todo era
boilerplate de remision), y los canales de persona del motor ("que sabes
sobre Fulano", "¿en que resoluciones aparece Fulano?") trataban esas
resoluciones como si fueran sobre ellos.

Heuristica (CONSERVADORA, por par persona-doc):

1. Solo se examinan personas con >= UMBRAL_DOCS_FIRMANTE docs en el indice:
   las autoridades habituales acumulan cientos de docs; por debajo del
   umbral el costo de un falso positivo (quitarle un doc legitimo a
   alguien) supera el beneficio y no se toca nada. Este umbral es tambien
   lo que separa al CANAL habitual (director que envia cientos de notas)
   del solicitante ocasional, que nunca se examina.
2. Una ocurrencia del nombre es "mencion de AUTORIDAD FIRMANTE" si cumple
   AMBAS:
   a) el cargo viene PEGADO tras el nombre ("Fulano ... Decano [en ejercicio
      de la] Facultad...", "Secretario Academico"). Exige facultad/academico
      para no confundir cargos de comision ("secretario de delegado...").
   b) esta en la ZONA DE FIRMA (despues de "Registrese, comuniquese y
      archivese", que siempre precede al pie de firmas) O viene del
      boilerplate de DELEGACION ("autoriza al ... la emision y firma de
      resoluciones", el OCR a veces da "finma").
3. Una ocurrencia es "mencion de REMITENTE INSTITUCIONAL" (v2) si viene del
   boilerplate de remision del VISTOS/CONSIDERANDO, en alguna de sus tres
   formas reales (ver las regex _RE_NOTA_POR_ANTES, _RE_POR_DIRECTOR_ANTES
   y _RE_MEDIANTE_NOTA_ANTES): "nota ... enviada/suscrita/presentada/
   remitida/emitida por el/la <hon.> Fulano, director...", la variante con
   el cargo ANTES del nombre ("por el director de la carrera de X
   Fulano..."), y "mediante nota <ref> Fulano, Ph.D. director de la carrera
   de X, ha solicitado...". Siempre exige el cargo director* adyacente: el
   universitario/docente que envia una nota solicitando algo PARA SI
   conserva el doc. "Renuncia" en la ventana previa desactiva la regla
   (la nota de renuncia del director SI es sobre el).
   Las menciones legitimas de cuerpo no cumplen 2 ni 3: el designado
   ("designar al ... como docente..." o "designar al ... director de la
   carrera..."), el beneficiario de un pago, el tribunal que presenta
   notas (sin cargo director), el solicitante sin cargo.
4. El doc se marca solo-firma/tramite para esa persona unicamente si TODAS
   sus ocurrencias (>= 1) son menciones tipo 2 o 3. Si el OCR esta tan roto
   que el nombre no se encuentra, NO se marca: en la duda la persona
   conserva el doc.

Con el corpus 2026-07 (3.232 docs) marca 866 pares en 16 personas: el
decano actual pasa de 214 a 105 docs (todo por tipo 2), el ex-director de
informatica de 536 a 149 (todo por tipo 3: sus designaciones, licencias,
viajes y menciones de cuerpo se conservan, igual que las notas con OCR
demasiado roto para matchear con confianza), y los demas directores de
carrera/instituto bajan en proporcion a sus notas de remision. Personas
legitimas con muchos docs (tribunales, docentes, tutores) quedan
intactas.

Salida: gaceta_rag/data/personas_firmantes.json — sidecar que
IndicePersonas resta al construirse (si no existe, todo queda como antes).
No usa LLM ni red; corre en segundos sobre chunks_v2.sqlite.

Uso:
    python firmantes.py              # escribe personas_firmantes.json
    python firmantes.py --dry-run    # solo reporta, no escribe
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE.parent / "data"
GRAFO_LLM = DATA / "grafo_llm.json"
CHUNKS = DATA / "chunks_v2.sqlite"
SALIDA = DATA / "personas_firmantes.json"

sys.path.insert(0, str(BASE))
from indice_personas import normalizar, tokens_nombre, es_institucion

UMBRAL_DOCS_FIRMANTE = 20

_RE_CARGO_TRAS = re.compile(
    r"^(?:\s*\w{1,3}){0,2}\s*(?:decan[oa]|vicedecan[oa]|secretari[oa])"
    r"(?:\s+\w+){0,4}\s+(?:facultad|academic[oa])\b")

_RE_CIERRE = re.compile(r"\b(?:registrese|comuniquese|archivese)\b")

_RE_DELEGACION_ANTES = re.compile(r"\b(?:autoriza\w*|emision)\b[^.]{0,50}$")

_RE_NOTA_POR_ANTES = re.compile(
    r"\bnotas?\b.{0,60}\b(?:enviad|suscrit|presentad|remitid|emitid)\w*"
    r"\s+por\s+(?:el|la)\b.{0,50}$")
_RE_POR_DIRECTOR_ANTES = re.compile(
    r"\b(?:enviad|suscrit|presentad|remitid|emitid)\w*\s+por\s+(?:el|la)\s+"
    r"(?:\w{1,3}\s+){0,3}director\w*\s+(?:de\s+)?(?:la\s+)?carrera"
    r"(?:\s+de\s+\w+)?\s*$")
_RE_MEDIANTE_NOTA_ANTES = re.compile(r"\bmediante\s+nota\b.{0,60}$")
_RE_DIRECTOR_TRAS = re.compile(r"^(?:\s*\w{1,3}){0,2}\s*director\w*\b")
_RE_DIRECTOR_CARRERA_TRAS = re.compile(
    r"^(?:\s*\w{1,3}){0,2}\s*director\w*(?:\s+\w+){0,4}\s+carrera\b")

_VENTANA_TRAS = 60
_VENTANA_ANTES = 70
_VENTANA_REMITENTE = 120

def _patrones_nombre(clave):
    """Subcadenas del nombre normalizado que delatan a la persona en el
    texto: nombre completo y los pares inicial/final ('aldo ramiro' /
    'valdez alvarado'), porque el OCR suele romper alguno de los tokens."""
    toks = clave.split()
    pats = {" ".join(toks)}
    if len(toks) >= 2:
        pats.add(" ".join(toks[:2]))
        pats.add(" ".join(toks[-2:]))
    return pats

def ocurrencias_nombre(clave, texto_norm):
    """Spans (inicio, fin) donde el nombre aparece en el texto normalizado,
    descartando los CONTENIDOS en otro mas largo (el subpatron 'aldo ramiro'
    dentro de 'aldo ramiro valdez alvarado' taparia el cargo que sigue)."""
    occ = set()
    for pat in _patrones_nombre(clave):
        for m in re.finditer(re.escape(pat), texto_norm):
            occ.add((m.start(), m.end()))
    occ = sorted(occ)
    return [(a, b) for a, b in occ
            if not any(a2 <= a and b <= b2 and (a2, b2) != (a, b)
                       for a2, b2 in occ)]

def _es_autoridad_firmante(a, b, texto_norm, zona_firma):
    """Mencion tipo 2: cargo de autoridad PEGADO tras el nombre, y ademas
    en la zona de firma o dentro del boilerplate de delegacion."""
    if not _RE_CARGO_TRAS.match(texto_norm[b:b + _VENTANA_TRAS]):
        return False
    if a >= zona_firma:
        return True
    return bool(_RE_DELEGACION_ANTES.search(
        texto_norm[max(0, a - _VENTANA_ANTES):a]))

def _es_remitente_institucional(a, b, texto_norm):
    """Mencion tipo 3: la persona ENVIA, como director de carrera/instituto,
    la nota de solicitud que la resolucion tramita (boilerplate del VISTOS/
    CONSIDERANDO). La resolucion no es sobre ella: es el canal."""
    antes = texto_norm[max(0, a - _VENTANA_REMITENTE):a]
    if "renuncia" in antes:
        return False
    tras = texto_norm[b:b + _VENTANA_TRAS]
    if _RE_NOTA_POR_ANTES.search(antes) and _RE_DIRECTOR_TRAS.match(tras):
        return True
    if _RE_POR_DIRECTOR_ANTES.search(antes):
        return True
    return bool(_RE_MEDIANTE_NOTA_ANTES.search(antes)
                and _RE_DIRECTOR_CARRERA_TRAS.match(tras))

def es_solo_firma(clave, texto_norm):
    """True si TODAS las ocurrencias del nombre en el doc son menciones de
    autoridad (firmante al pie/delegacion, o remitente institucional).
    False si hay al menos una mencion de cuerpo o si el nombre no se
    encuentra."""
    m_cierre = _RE_CIERRE.search(texto_norm)
    zona_firma = m_cierre.end() if m_cierre else len(texto_norm) + 1
    occ = ocurrencias_nombre(clave, texto_norm)
    if not occ:
        return False
    return all(_es_autoridad_firmante(a, b, texto_norm, zona_firma)
               or _es_remitente_institucional(a, b, texto_norm)
               for a, b in occ)

def construir_firmantes(ruta_grafo=GRAFO_LLM, ruta_chunks=CHUNKS,
                        umbral=UMBRAL_DOCS_FIRMANTE, verbose=False):
    """{clave normalizada -> [doc_id donde SOLO firma/tramita]} para las
    personas con >= umbral docs en grafo_llm.json. Mismas claves que
    IndicePersonas (tokens_nombre), asi el indice puede restarlas 1:1."""
    docs = json.load(open(ruta_grafo, encoding="utf-8")).get("docs", {})
    por_persona = {}
    for did in sorted(docs):
        for p in docs[did].get("personas") or []:
            if es_institucion(p):
                continue
            clave = " ".join(tokens_nombre(p))
            if clave:
                por_persona.setdefault(clave, [])
                if did not in por_persona[clave]:
                    por_persona[clave].append(did)

    conn = sqlite3.connect(str(ruta_chunks))
    cache = {}

    def texto_norm(did):
        if did not in cache:
            filas = conn.execute(
                "SELECT texto FROM chunks WHERE doc_id = ? ORDER BY chunk_id",
                (did,)).fetchall()
            cache[did] = normalizar("\n".join(f[0] for f in filas))
        return cache[did]

    firmantes = {}
    for clave, dids in sorted(por_persona.items(), key=lambda kv: -len(kv[1])):
        if len(dids) < umbral:
            continue
        solo = [d for d in dids if es_solo_firma(clave, texto_norm(d))]
        if solo:
            firmantes[clave] = solo
        if verbose:
            print(f"  {len(dids):4d} -> quedan {len(dids) - len(solo):4d} "
                  f"(solo-firma {len(solo):4d})  {clave}")
    conn.close()
    return firmantes

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Marca pares persona-doc donde la persona SOLO firma "
                    "o tramita.")
    ap.add_argument("--dry-run", action="store_true",
                    help="solo reporta, no escribe el JSON")
    ap.add_argument("--salida", default=str(SALIDA))
    args = ap.parse_args(argv)

    if not GRAFO_LLM.exists() or not CHUNKS.exists():
        print("[firmantes] falta grafo_llm.json o chunks_v2.sqlite; nada que hacer")
        return 0

    firmantes = construir_firmantes(verbose=True)
    n_pares = sum(len(v) for v in firmantes.values())
    print(f"[firmantes] {len(firmantes)} personas con docs solo-firma "
          f"({n_pares} pares persona-doc)")
    if args.dry_run:
        return 0
    salida = {
        "_meta": {
            "descripcion": "docs donde la persona SOLO firma o tramita "
                           "(ver firmantes.py)",
            "umbral_docs": UMBRAL_DOCS_FIRMANTE,
            "fuente": "grafo_llm.json + chunks_v2.sqlite",
        },
        "firmantes": firmantes,
    }
    with open(args.salida, "w", encoding="utf-8") as f:
        json.dump(salida, f, ensure_ascii=False, indent=1)
    print(f"-> {args.salida}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
