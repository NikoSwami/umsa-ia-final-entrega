# -*- coding: utf-8 -*-
"""
Hechos VERIFICADOS de una unidad del organigrama, para que el modelo redacte
con ellos.

Para que sirve
──────────────
"¿De que se encarga X?", "¿que funciones tiene?", "¿que cargos?", "¿que
resolucion la creo?" piden redactar, y eso lo hace el LLM. Pero sin los datos
de la unidad delante, el modelo los reconstruye con lo que encuentra: en la
linea base atribuia a la unidad lo que decian resoluciones de otras cosas y
citaba numeros que no eran de ella. Esto le pone delante, en un formato fijo,
lo que MOF dice de la unidad; NOTA_ORGANIGRAMA le prohibe deducir el resto.

  UNIDAD: <nombre> (<codigo>)
  DEPENDE DE: <padre>
  UNIDADES A SU CARGO: <n> directas, <m> en toda la rama
  FUNCIONES (<k>):
  1. ...                       (hasta TOPE_FUNCIONES, despues "y N mas")
  CARGOS: ...
  CREADA POR: <res_creacion>
  [RESOLUCION DE CREACION EN LA GACETA: ...]   (para "creacion", si la hay)
  DATOS DE MOF AL: <fecha>

Todo sale de replica.py (unidad, hijas_de, descendientes_de, funciones_de,
cargos_de), y la resolucion de la Gaceta, de la arista "crea" que escribe
enlaces.py en grafo_citas.json. Lo que MOF no trae se dice ("no figura"), en
vez de dejar una linea vacia que el modelo se sienta invitado a completar.

Los mismos hechos, dos veces
────────────────────────────
datos() los devuelve como diccionario y de ahi salen DOS textos: el bloque
que lee el modelo y la plantilla de respaldo que le contesta al usuario
cuando la redaccion se descarta (respuestas.respaldo). Una sola consulta a la
replica para los dos, asi la plantilla no puede decir un numero que el bloque
no traiga.

Verificar lo que redacto el modelo
──────────────────────────────────
numeros_ajenos() compara las cifras de la respuesta con las del bloque: si
aparece una que no esta ahi, el motor descarta la redaccion (motor_web, paso
10). Los ordinales de una lista no cuentan de ningun lado: numeran lineas, no
son datos de la unidad —y que el bloque numere su tercera funcion "3." no
convierte al 3 en un dato suyo—.
"""
import re
from datetime import datetime

from . import exportador, replica
from . import vigencia as _vigencia

TOPE_FUNCIONES = 15
TIPOS = frozenset({"describir", "funciones", "cargos", "creacion"})
NO_FIGURA = "no figura en el organigrama de MOF"
NO_FIGURAN = "no figuran en el organigrama de MOF"

def datos(conn, unidad_id, grafo=None, creacion=False, vigencia=None) -> dict:
    """Los hechos de la unidad, o None si no esta en la replica.

    grafo    : GrafoCitas ya cargado (grafo_citas.json), para "creacion".
    creacion : busca la resolucion de la Gaceta que creo la unidad.
    vigencia : de cuando son los datos segun la consulta a MOF
               (vigencia.de_la_consulta). Sin ella, o sin consulta ok, la
               fecha es la mas nueva de las filas usadas."""
    unidad = replica.unidad(conn, unidad_id)
    if unidad is None:
        return None
    hijas = replica.hijas_de(conn, unidad_id)
    rama = replica.descendientes_de(conn, unidad_id)
    return {
        "id": unidad_id,
        "nombre": _linea(unidad["nombre"]),
        "codigo": _linea(unidad["codigo"]),
        "depende_de": _padre(conn, unidad) if unidad["padre_id"] else None,
        "directas": len(hijas),
        "rama": len(rama),
        "funciones": [_linea(f["funcion"])
                      for f in replica.funciones_de(conn, unidad_id)],
        "cargos": [_linea(c["nombre"])
                   for c in replica.cargos_de(conn, unidad_id) if _linea(c["nombre"])],
        "creada_por": _linea(unidad["res_creacion"]),
        "en_la_gaceta": _en_la_gaceta(grafo, unidad_id) if creacion else None,
        "fecha": ((vigencia.fecha if vigencia is not None else None)
                  or _fecha([unidad, *hijas, *rama])),
        "aviso": bool(vigencia is not None and vigencia.aviso),
    }

def bloque(hechos: dict) -> str:
    """El bloque de hechos tal como lo lee el modelo, desde datos()."""
    funciones = hechos["funciones"]
    lineas = [
        f"UNIDAD: {hechos['nombre']} ({hechos['codigo'] or 'sin código'})",
        f"DEPENDE DE: {hechos['depende_de'] or 'ninguna (es la cima del organigrama)'}",
        f"UNIDADES A SU CARGO: {hechos['directas']} directas, "
        f"{hechos['rama']} en toda la rama",
        f"FUNCIONES ({len(funciones)}):",
    ]
    if funciones:
        lineas += [f"{k}. {f}" for k, f in enumerate(funciones[:TOPE_FUNCIONES], 1)]
        if len(funciones) > TOPE_FUNCIONES:
            lineas.append(f"y {len(funciones) - TOPE_FUNCIONES} más")
    else:
        lineas.append(f"({NO_FIGURAN})")
    lineas.append("CARGOS: " + (", ".join(hechos["cargos"]) if hechos["cargos"]
                                else NO_FIGURAN))
    lineas.append(f"CREADA POR: {hechos['creada_por'] or NO_FIGURA}")
    if hechos["en_la_gaceta"]:
        lineas.append(f"RESOLUCIÓN DE CREACIÓN EN LA GACETA: {hechos['en_la_gaceta']}")
    lineas.append(f"DATOS DE MOF AL: {hechos['fecha'] or 'sin fecha'}"
                  + (f" ({_vigencia.AVISO})" if hechos.get("aviso") else ""))
    return "\n".join(lineas)

def bloque_hechos(conn, unidad_id, grafo=None, creacion=False):
    """Texto del bloque, o None si la unidad no esta en la replica."""
    hechos = datos(conn, unidad_id, grafo=grafo, creacion=creacion)
    return None if hechos is None else bloque(hechos)

_ORDINAL = re.compile(r"^(\s*(?:#{1,6}\s*)?(?:[-*•+]\s+)?)\d{1,3}[.)](?=\s)", re.M)

def numeros_ajenos(respuesta, texto_del_bloque) -> list:
    """Numeros de la respuesta que NO estan en el bloque de hechos, en el
    orden en que aparecen y sin repetir. Se comparan como enteros: la
    resolucion "048/2024" del bloque cubre a "48/2024" de la respuesta, y el
    año y el numero de resolucion valen porque figuran ahi."""
    del_bloque = set(_numeros(texto_del_bloque))
    ajenos = []
    for numero in _numeros(respuesta):
        if numero not in del_bloque and numero not in ajenos:
            ajenos.append(numero)
    return ajenos

def _numeros(texto) -> list:
    """Los numeros de un texto (sin los ordinales de lista), como enteros."""
    return [int(x) for x in
            re.findall(r"\d+", _ORDINAL.sub(r"\1", str(texto or "")))]

def chunk(texto, unidad_id, unidad_nombre) -> dict:
    """El bloque como un chunk mas del contexto. El doc_id es el de la ficha de
    la unidad: si el modelo cita "[doc_9000NN]", reemplazar_doc_ids_por_cites
    lo convierte en la etiqueta del organigrama."""
    return {"texto": texto,
            "doc_id": exportador.doc_id_de_unidad(unidad_id),
            "cite_label": f"Organigrama MOF: {unidad_nombre}"}

def _linea(texto) -> str:
    """Una sola linea: una funcion con saltos de linea rompe el formato fijo."""
    return re.sub(r"\s+", " ", str(texto or "")).strip()

def _padre(conn, unidad) -> str:
    """Nombre de la unidad de la que depende (ya se sabe que tiene una)."""
    nombre = _linea(unidad["padre_nombre"])
    if not nombre:
        padre = replica.unidad(conn, unidad["padre_id"])
        nombre = _linea(padre["nombre"]) if padre is not None else ""
    return nombre or NO_FIGURA

def _en_la_gaceta(grafo, unidad_id):
    """Resoluciones del corpus con la arista 'crea' desde la ficha de la
    unidad (gaceta_rag/mof/enlaces.py), o None."""
    if grafo is None:
        return None
    ficha = exportador.doc_id_de_unidad(unidad_id)
    etiquetas = []
    for doc_id, relacion in sorted((getattr(grafo, "rel", {}) or {}).get(ficha, {}).items()):
        if relacion != "crea":
            continue
        org, num, anio = (getattr(grafo, "ident", {}) or {}).get(doc_id, (None, None, None))
        if num and anio:
            etiquetas.append(f"Resolución {org + ' ' if org else ''}{num}/{anio}")
    return "; ".join(etiquetas) or None

def _fecha(filas):
    """La mas nueva de las filas, "dd/mm/YYYY HH:MM", o None si no hay."""
    fechas = [f["actualizado"] for f in filas if f["actualizado"]]
    if not fechas:
        return None
    ultima = max(fechas)
    try:
        return datetime.fromisoformat(ultima).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return ultima
