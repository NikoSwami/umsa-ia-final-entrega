# -*- coding: utf-8 -*-
"""Convierte la salida de PaddleOCR-VL a Markdown puro, y el Markdown a texto
para indexar.

La primera mitad (a_markdown, tabla_a_pipes, reubicar_imagenes, contar_filas)
viene de local/a_markdown.py del proyecto
PaddleOCR-VL (MIT), el mismo OCR que se
probo en Colab: el modelo devuelve las tablas como <table><tr><td> y deja
<div>/<br> sueltos, y eso no es Markdown. Un solo agregado: sin imagenes,
tambien se quita la etiqueta <img> que el modelo deja dentro de un <div>.

La segunda (markdown_a_texto) es propia de la Gaceta: lo que va al indice no
necesita el formato, necesita el texto. Se conservan las tablas (filas de
pipes, que el LLM lee bien) y se quita lo que solo es marcado.
"""

from __future__ import annotations

import html
import os
import re

RE_TABLA = re.compile(r'<table\b.*?</table>', re.S | re.I)
RE_FILA = re.compile(r'<tr\b[^>]*>(.*?)</tr>', re.S | re.I)
RE_CELDA = re.compile(r'<(t[hd])\b([^>]*)>(.*?)</\1>', re.S | re.I)
RE_COLSPAN = re.compile(r'colspan\s*=\s*["\']?(\d+)', re.I)
RE_ROWSPAN = re.compile(r'rowspan\s*=\s*["\']?(\d+)', re.I)
RE_IMAGEN = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')
RE_BR = re.compile(r'<br\s*/?>', re.I)
RE_SOBRAS = re.compile(
    r'</?(div|span|p|font|b|i|u|em|strong|sup|sub|center|a|html|body|head)\b[^>]*>',
    re.I)

def _limpiar(bruto):
    texto = RE_BR.sub(' ', bruto)
    texto = re.sub(r'<[^>]+>', '', texto)
    texto = html.unescape(texto)
    return ' '.join(texto.split()).replace('|', r'\|')

def _celdas(fila):
    """[(texto, colspan, rowspan)] de una fila HTML."""
    salida = []
    for etiqueta, atributos, contenido in RE_CELDA.findall(fila):
        ancho = RE_COLSPAN.search(atributos)
        alto = RE_ROWSPAN.search(atributos)
        salida.append((_limpiar(contenido), int(ancho.group(1)) if ancho else 1,
                       int(alto.group(1)) if alto else 1))
    return salida

def tabla_a_pipes(bloque):
    """Pasa una tabla HTML a una tabla Markdown de pipes.

    Markdown no tiene celdas combinadas: una celda con colspan deja celdas
    vacias a su derecha y una con rowspan las deja DEBAJO, para que el resto
    de la fila quede en su columna. El original solo hacia lo primero: en un
    encabezado de dos niveles ("Costo" sobre "Matricula | Mensualidad", con
    "Programa" ocupando dos filas) la segunda fila quedaba corrida una
    columna a la izquierda."""
    ocupadas = set()
    filas = []
    for html_fila in RE_FILA.findall(bloque):
        celdas = _celdas(html_fila)
        if not celdas:
            continue
        r, fila, c = len(filas), [], 0
        for texto, colspan, rowspan in celdas:
            while (r, c) in ocupadas:
                fila.append('')
                c += 1
            fila.append(texto)
            fila.extend([''] * (colspan - 1))
            for dc in range(colspan):
                for dr in range(1, rowspan):
                    ocupadas.add((r + dr, c + dc))
            c += colspan
        while (r, c) in ocupadas:
            fila.append('')
            c += 1
        filas.append(fila)
    if not filas:
        return ''
    ancho = max(len(f) for f in filas)
    filas = [f + [''] * (ancho - len(f)) for f in filas]
    if not any(c.strip() for c in filas[0]):
        filas[0] = [f'col {i + 1}' for i in range(ancho)]
    lineas = ['| ' + ' | '.join(filas[0]) + ' |', '|' + ' --- |' * ancho]
    lineas += ['| ' + ' | '.join(f) + ' |' for f in filas[1:]]
    return '\n'.join(lineas)

def reubicar_imagenes(texto, base_pieza, base_destino):
    """Reescribe los `![](imgs/...)` para que apunten bien desde el destino."""
    def cambiar(m):
        destino = m.group(2).strip()
        if destino.startswith(('http://', 'https://', 'data:', '/')):
            return m.group(0)
        absoluta = os.path.normpath(os.path.join(base_pieza, destino))
        relativa = os.path.relpath(absoluta, base_destino).replace(os.sep, '/')
        return f'![{m.group(1)}]({relativa})'
    return RE_IMAGEN.sub(cambiar, texto)

def a_markdown(texto, imagenes=True):
    """Deja Markdown puro: tablas de pipes, sin HTML suelto."""
    texto = RE_TABLA.sub(lambda m: '\n' + tabla_a_pipes(m.group(0)) + '\n', texto)
    if not imagenes:
        texto = RE_IMAGEN.sub('', texto)
        texto = RE_IMG_HTML.sub('', texto)
    texto = RE_BR.sub('\n', texto)
    texto = RE_SOBRAS.sub('', texto)
    texto = html.unescape(texto)
    texto = re.sub(r'[ \t]+\n', '\n', texto)
    texto = re.sub(r'\n{3,}', '\n\n', texto)
    return texto.strip()

def contar_filas(texto):
    return sum(1 for l in texto.splitlines()
               if l.lstrip().startswith('|') and not set(l) <= set('| -'))

RE_IMG_HTML = re.compile(r'<img\b[^>]*>', re.I)
RE_COMENTARIO = re.compile(r'<!--.*?-->', re.S)
RE_TITULO_MD = re.compile(r'^[ \t]{0,3}#{1,6}[ \t]+', re.M)
RE_NEGRITA = re.compile(r'(\*\*|__)(?=\S)(.+?)(?<=\S)\1', re.S)
RE_SEPARADOR_TABLA = re.compile(r'^\s*\|?(\s*:?-{3,}:?\s*\|)+\s*:?-{0,}:?\s*\|?\s*$')

def markdown_a_texto(md: str) -> str:
    """Texto para indexar a partir del Markdown del OCR.

    Quita lo que solo es marcado (titulos #, negritas, imagenes, comentarios,
    la fila separadora de cada tabla) y conserva el contenido, incluidas las
    filas de las tablas: "| Materia | Creditos |" el LLM lo lee bien, y
    aplanarlas perderia que dato va con cual.
    """
    if not md:
        return ''
    texto = a_markdown(md, imagenes=False)
    texto = RE_COMENTARIO.sub('', texto)
    texto = RE_TITULO_MD.sub('', texto)
    texto = RE_NEGRITA.sub(r'\2', texto)
    lineas = [l for l in texto.splitlines() if not RE_SEPARADOR_TABLA.match(l)]
    texto = '\n'.join(lineas)
    texto = re.sub(r'[ \t]+\n', '\n', texto)
    texto = re.sub(r'\n{3,}', '\n\n', texto)
    return texto.strip()
