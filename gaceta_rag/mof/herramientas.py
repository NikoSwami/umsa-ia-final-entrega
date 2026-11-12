# -*- coding: utf-8 -*-
"""
Herramientas de SOLO LECTURA sobre la replica del organigrama, para un modelo
por API que sabe llamarlas (motor_api, con GACETA_API_HERRAMIENTAS=1).

Son cinco funciones de replica.py tal cual —contar_unidades, hijas_de,
descendientes_de, funciones_de y buscar— con su descripcion en el formato de
"tools" del dialecto OpenAI. El bucle que las ofrece y las ejecuta vive en
motor_api.ClienteAPI; esto solo sabe que hace cada una.

Reglas
──────
- Cada llamada abre la replica con replica.abrir_lectura() y la cierra. Nunca
  la conexion de la ingesta, nunca una escritura.
- Un error vuelve como {"error": ...}: el modelo lo lee y sigue (o se
  disculpa); no tumba el chat.
- Las listas se cortan en TOPE, pero el total va siempre entero.
- Los ids internos viajan (el modelo los necesita para encadenar llamadas),
  pero no son datos del organigrama: verificable() los saca antes de contar
  que numeros le dio la replica al modelo.
"""
import json

from . import replica

TOPE = 40

_UNIDAD_ID = {"unidad_id": {"type": "integer",
                            "description": "id de la unidad; lo devuelve buscar"}}

DEFINICIONES = [
    {"type": "function", "function": {
        "name": "contar_unidades",
        "description": "Cuantas unidades tiene el organigrama de MOF en total.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "hijas_de",
        "description": ("Unidades que dependen DIRECTAMENTE de una unidad "
                        "(solo el primer nivel), con el total."),
        "parameters": {"type": "object", "properties": _UNIDAD_ID,
                       "required": ["unidad_id"]}}},
    {"type": "function", "function": {
        "name": "descendientes_de",
        "description": ("Todas las unidades de la rama de una unidad, a "
                        "cualquier nivel de profundidad, con el total."),
        "parameters": {"type": "object", "properties": _UNIDAD_ID,
                       "required": ["unidad_id"]}}},
    {"type": "function", "function": {
        "name": "funciones_de",
        "description": "Las funciones de una unidad segun el MOF, con el total.",
        "parameters": {"type": "object", "properties": _UNIDAD_ID,
                       "required": ["unidad_id"]}}},
    {"type": "function", "function": {
        "name": "buscar",
        "description": ("Busca unidades por nombre o sigla (sin importar "
                        "acentos ni mayusculas). Devuelve id y nombre de cada una."),
        "parameters": {"type": "object",
                       "properties": {"texto": {"type": "string",
                                                "description": "parte del nombre"}},
                       "required": ["texto"]}}},
]
NOMBRES = frozenset(d["function"]["name"] for d in DEFINICIONES)

def ejecutar(nombre, argumentos, ruta=None) -> dict:
    """El resultado de UNA herramienta, listo para serializar."""
    if nombre not in NOMBRES:
        return {"error": f"no existe la herramienta {nombre!r}"}
    try:
        conn = replica.abrir_lectura(ruta)
    except Exception as exc:
        return {"error": f"el organigrama no esta disponible ({exc.__class__.__name__})"}
    try:
        return _HACER[nombre](conn, argumentos or {})
    except (KeyError, TypeError, ValueError) as exc:
        return {"error": f"argumentos invalidos para {nombre}: {exc}"}
    finally:
        conn.close()

def verificable(resultado) -> str:
    """El resultado sin los ids internos, como texto: lo que cuenta como cifra
    de la replica al verificar la respuesta del modelo."""
    return json.dumps(_sin_ids(resultado), ensure_ascii=False)

def _contar_unidades(conn, argumentos):
    return {"unidades": replica.contar_unidades(conn)}

def _de_una_unidad(listar):
    def hacer(conn, argumentos):
        unidad_id = int(argumentos["unidad_id"])
        unidad = replica.unidad(conn, unidad_id)
        if unidad is None:
            return {"error": f"no hay ninguna unidad con id {unidad_id}"}
        return dict({"unidad": unidad["nombre"]}, **listar(conn, unidad_id))
    return hacer

def _unidades(filas) -> dict:
    listado = {"total": len(filas),
               "unidades": [{"id": f["id"], "nombre": f["nombre"]} for f in filas[:TOPE]]}
    if len(filas) > TOPE:
        listado["no_listadas"] = len(filas) - TOPE
    return listado

def _funciones(conn, unidad_id) -> dict:
    filas = replica.funciones_de(conn, unidad_id)
    listado = {"total": len(filas),
               "funciones": [" ".join(str(f["funcion"] or "").split()) for f in filas[:TOPE]]}
    if len(filas) > TOPE:
        listado["no_listadas"] = len(filas) - TOPE
    return listado

def _buscar(conn, argumentos):
    texto = str(argumentos["texto"]).strip()
    if not texto:
        raise ValueError("falta el texto a buscar")
    return {"unidades": [{"id": f["id"], "nombre": f["nombre"]}
                         for f in replica.buscar(conn, texto, limite=TOPE)]}

_HACER = {
    "contar_unidades": _contar_unidades,
    "hijas_de": _de_una_unidad(lambda conn, uid: _unidades(replica.hijas_de(conn, uid))),
    "descendientes_de": _de_una_unidad(
        lambda conn, uid: _unidades(replica.descendientes_de(conn, uid))),
    "funciones_de": _de_una_unidad(_funciones),
    "buscar": _buscar,
}

def _sin_ids(valor):
    if isinstance(valor, dict):
        return {k: _sin_ids(v) for k, v in valor.items() if k not in ("id", "unidad_id")}
    if isinstance(valor, list):
        return [_sin_ids(v) for v in valor]
    return valor
