# -*- coding: utf-8 -*-
"""
Rango de doc_id para los documentos cargados a mano.

El doc_id NO sale del nombre del archivo: generar_base.py lo arma como
f"doc_{paperlessId:05d}" y solo recorre doc_*.json. Un documento cargado a
mano tiene que llevar un paperlessId propio que coincida con el nombre de su
archivo, o la proxima reconstruccion completa lo indexaria con otro id (o con
doc_00000) y todas sus citas se romperian. Es la misma leccion que dejo MOF.

Por que un rango aparte y no "el siguiente numero de Paperless":
  - Paperless sigue numerando por su cuenta. Si aqui se tomara el 3313, el
    proximo export de Paperless traeria OTRO documento con el 3313 y lo
    pisaria en silencio.
  - El rango se reconoce a simple vista (doc_1xxxxx) y se puede listar o
    borrar en bloque, como las fichas de MOF (doc_9xxxxx).

  doc_00001 .. doc_99999    corpus de Paperless
  doc_100001 .. doc_899999  carga manual (este modulo)
  doc_900001 .. doc_999999  fichas del organigrama (gaceta_rag/mof)

Recencia: el boost de recencia del motor supone ids cronologicos. Un
documento cargado a mano es, por definicion, lo ultimo que entro a la base:
se lo trata como el mas reciente del corpus, sin que su numero (100.001+)
entre en la escala (ver consultar_gaceta.aplicar_boost_recencia).
"""
import re
from pathlib import Path

RANGO_BASE = 100_000
RANGO_MAX = 799_999

_RE_DOC = re.compile(r"doc_(\d+)")

def numero_de(doc_id):
    """doc_100012 -> 100012; None si no tiene la forma doc_N."""
    m = _RE_DOC.fullmatch(str(doc_id or ""))
    return int(m.group(1)) if m else None

def es_carga_manual(doc_id) -> bool:
    """True si el doc_id esta en el rango de la carga manual."""
    n = numero_de(doc_id)
    return n is not None and RANGO_BASE < n <= RANGO_BASE + RANGO_MAX

def doc_id_de(paperless_id) -> str:
    """Mismo formato que generar_base: f"doc_{pid:05d}"."""
    return f"doc_{int(paperless_id):05d}"

def siguiente_doc_id(carpeta_raw, reservados=(), ultimo_entregado=None) -> str:
    """El siguiente doc_id libre del rango manual: siempre maximo + 1.

    reservados       : ids tomados por trabajos en curso que todavia no
                       escribieron su raw (dos cargas confirmadas a la vez no
                       pueden recibir el mismo numero).
    ultimo_entregado : el mayor id que se entrego alguna vez, aunque su
                       documento ya se haya borrado. Sin el, borrar el ultimo
                       documento haria que el siguiente heredara su numero, y
                       una cita vieja a ese id pasaria a nombrar a otro.
    """
    usados = [numero_de(d) for d in reservados]
    if ultimo_entregado:
        usados.append(numero_de(ultimo_entregado))
    carpeta = Path(carpeta_raw)
    if carpeta.is_dir():
        usados += [numero_de(p.stem) for p in carpeta.glob("doc_*.json")]
    usados = [n for n in usados
              if n is not None and RANGO_BASE < n <= RANGO_BASE + RANGO_MAX]
    siguiente = (max(usados) if usados else RANGO_BASE) + 1
    if siguiente > RANGO_BASE + RANGO_MAX:
        raise RuntimeError("Se agoto el rango de doc_id de la carga manual.")
    return doc_id_de(siguiente)
