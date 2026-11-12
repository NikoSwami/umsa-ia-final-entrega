# -*- coding: utf-8 -*-
"""
Despues de indexar: el grafo y los indices derivados.

Un documento recien indexado ya es buscable por texto y por significado.
Lo que todavia no tiene es lo que sale del grafo LLM: tema, personas y citas
(que resolucion deroga o modifica a cual). Sin eso no aparece en "¿en que
resoluciones figura Fulano?", ni trae a las que lo modifican.

Son los mismos pasos que construir_todo.py, sin la base vectorial (eso ya lo
hizo la ingesta en caliente):

  1. LLM sobre el documento -> su entrada en grafo_llm.json. Se usa el LLM que
     el motor YA tiene cargado (la API, o Qwen): cargar un segundo Qwen no
     entra en la GPU, y por API es una llamada por documento.
  2. identidad, adyacencia, firmantes y enlaces de MOF: los scripts de siempre,
     sin GPU, en el mismo orden (pasos_derivados()).
  3. recargar grafo, personas y temas en el motor que esta atendiendo.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from gaceta_rag import generar_base as gb
from gaceta_rag.mejoras import identidad

RAIZ = Path(__file__).resolve().parents[2]
DATA = RAIZ / 'gaceta_rag' / 'data'
GRAFO_LLM = DATA / 'grafo_llm.json'
MAX_CHARS = 3000

_lock_archivo = threading.Lock()

def pasos_derivados(py=None, raiz=RAIZ) -> list:
    """[(nombre, cmd)] que siguen al LLM, en el orden de construir_todo.plan_pasos.

    Hay un test que compara esta lista con plan_pasos(): si alguien agrega un
    paso alla, este se entera."""
    py = py or sys.executable
    mejoras = Path(raiz) / 'gaceta_rag' / 'mejoras'
    return [
        ('Identidad del titulo sobre grafo_llm.json',
         [py, str(mejoras / 'corregir_identidad_grafo.py')]),
        ('Grafo de citas (adyacencia desde grafo_llm.json)',
         [py, str(mejoras / 'construir_adyacencia.py')]),
        ('Firmantes (personas_firmantes.json)',
         [py, str(mejoras / 'firmantes.py')]),
        ('Enlaces MOF (unidad -> resolucion que la crea)',
         [py, '-m', 'gaceta_rag.mof.enlaces']),
    ]

def _json_de_respuesta(texto: str) -> dict:
    """El JSON de la respuesta del LLM.

    Qwen con response_format devuelve JSON pelado; algunos modelos por API lo
    envuelven en ```json ... ``` o le agregan una frase. Se toma el primer
    objeto completo."""
    texto = (texto or '').strip()
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        pass
    m = re.search(r'\{.*\}', texto, re.S)
    if not m:
        raise ValueError(f'la respuesta no trae JSON: {texto[:120]!r}')
    return json.loads(m.group(0))

def extraer_hechos(llm, doc: dict, max_chars: int = MAX_CHARS,
                   candado=None) -> dict:
    """La entrada de grafo_llm.json de un documento (como extraer_grafo_llm).

    candado: el lock del motor. Qwen local no es reentrante: si el chat esta
    generando, hay que esperar a que termine."""
    contenido = doc.get('contenido') or ''
    mensajes = identidad.construir_mensajes(doc.get('titulo'), contenido,
                                            max_chars)
    try:
        if candado is not None:
            with candado:
                r = llm.create_chat_completion(
                    messages=mensajes, max_tokens=380, temperature=0.0,
                    response_format={'type': 'json_object'})
        else:
            r = llm.create_chat_completion(
                messages=mensajes, max_tokens=380, temperature=0.0,
                response_format={'type': 'json_object'})
        j = _json_de_respuesta(r['choices'][0]['message']['content'])
        if not isinstance(j, dict):
            raise ValueError('el JSON no es un objeto')
        identidad.aplicar_identidad(doc, j)
    except Exception as exc:
        j = {'_error': str(exc)[:120]}
    j['_hash'] = gb.hash_contenido(contenido)
    return j

def _leer(ruta) -> dict:
    ruta = Path(ruta)
    if not ruta.exists():
        return {'meta': {}, 'docs': {}}
    with open(ruta, encoding='utf-8') as f:
        grafo = json.load(f)
    grafo.setdefault('meta', {})
    grafo.setdefault('docs', {})
    return grafo

def _escribir(grafo: dict, ruta) -> None:
    """Igual que extraer_grafo_llm.guardar_atomico: .tmp + replace."""
    ruta = Path(ruta)
    grafo['meta']['docs'] = len(grafo['docs'])
    grafo['meta']['actualizado'] = datetime.datetime.now().isoformat(timespec='seconds')
    tmp = ruta.with_suffix('.json.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(grafo, f, ensure_ascii=False)
    os.replace(tmp, ruta)

def guardar_hechos(nuevos: dict, ruta=GRAFO_LLM) -> None:
    """Agrega o reemplaza entradas {doc_id: hechos} en grafo_llm.json."""
    if not nuevos:
        return
    with _lock_archivo:
        grafo = _leer(ruta)
        grafo['docs'].update(nuevos)
        _escribir(grafo, ruta)

def quitar(doc_ids, ruta=GRAFO_LLM) -> int:
    """Saca documentos de grafo_llm.json. Devuelve cuantos habia."""
    with _lock_archivo:
        grafo = _leer(ruta)
        n = 0
        for d in doc_ids:
            if grafo['docs'].pop(d, None) is not None:
                n += 1
        if n:
            _escribir(grafo, ruta)
        return n

def en_grafo(ruta=GRAFO_LLM) -> dict:
    """{doc_id: hechos} de grafo_llm.json ({} si no existe o no se lee)."""
    try:
        return _leer(ruta)['docs']
    except Exception:
        return {}

def rehacer_derivados(ejecutar=None, py=None, raiz=RAIZ) -> list:
    """Corre los pasos derivados. Devuelve [{paso, ok, codigo, salida}].

    No se corta al primer fallo: un paso que falla deja los demas igual de
    utiles (p. ej. sin MOF, los enlaces no tienen nada que hacer)."""
    ejecutar = ejecutar or _ejecutar
    resultados = []
    for nombre, cmd in pasos_derivados(py, raiz):
        codigo, salida = ejecutar(cmd, raiz)
        resultados.append({'paso': nombre, 'ok': codigo == 0, 'codigo': codigo,
                           'salida': salida[-800:]})
    return resultados

def _ejecutar(cmd, raiz):
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    r = subprocess.run(cmd, cwd=str(raiz), capture_output=True, text=True,
                       encoding='utf-8', errors='replace', env=env)
    return r.returncode, (r.stdout or '') + (r.stderr or '')

def recargar_indices(motor) -> dict:
    """Vuelve a leer grafo, personas y temas en el motor vivo.

    Se construye el objeto nuevo entero y recien entonces se reemplaza la
    referencia: una consulta en curso sigue con el viejo y la siguiente usa el
    nuevo, sin ver nunca uno a medio cargar. Cada uno respeta su interruptor
    (USAR_GRAFO_CITAS, ...) igual que motor_web.cargar()."""
    import motor_web
    hecho = {}
    candidatos = (
        ('grafo', motor_web.USAR_GRAFO_CITAS,
         'gaceta_rag.mejoras.grafo_citas', 'GrafoCitas'),
        ('personas', motor_web.USAR_INDICE_PERSONAS,
         'gaceta_rag.mejoras.indice_personas', 'IndicePersonas'),
        ('temas', motor_web.USAR_INDICE_TEMAS,
         'gaceta_rag.mejoras.indice_temas', 'IndiceTemas'),
    )
    import importlib
    for atributo, activo, modulo, clase in candidatos:
        if not activo:
            hecho[atributo] = 'desactivado'
            continue
        try:
            nuevo = getattr(importlib.import_module(modulo), clase)()
            if getattr(nuevo, 'disponible', False):
                setattr(motor, atributo, nuevo)
                hecho[atributo] = 'recargado'
            else:
                hecho[atributo] = 'sin datos'
        except Exception as exc:
            hecho[atributo] = f'error: {exc}'
    return hecho
