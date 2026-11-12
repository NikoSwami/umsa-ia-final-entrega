# -*- coding: utf-8 -*-
"""
Archivo subido -> texto para indexar.

Un PDF de la Gaceta puede ser de dos clases, y tratarlos igual sale caro:

  - DIGITAL (exportado de Word): ya trae una capa de texto exacta. Leerla es
    instantaneo y no se equivoca en ningun acento. Pasarlo por OCR seria
    minutos de CPU para obtener un texto PEOR.
  - ESCANEADO (la mayoria del corpus): es una foto por pagina, sin texto. Solo
    el OCR lo lee.

El modo 'auto' decide por documento mirando la capa de texto: si casi todas
las paginas traen texto de verdad, se usa; si no, OCR. 'ocr' y 'texto'
fuerzan uno u otro (por ejemplo, un PDF con una capa de texto basura que dejo
otro OCR viejo: ahi conviene forzar 'ocr').
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import a_markdown
from .ocr import ErrorOCR

PDF = {'.pdf'}
IMAGENES = {'.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.webp'}
TEXTOS = {'.txt', '.md'}
JSONS = {'.json'}
EXTENSIONES = sorted(PDF | IMAGENES | TEXTOS | JSONS)

MODOS = ('auto', 'ocr', 'texto')

MIN_ALNUM_POR_PAGINA = 40
PROPORCION_PAGINAS_CON_TEXTO = 0.8

class ErrorExtraccion(ValueError):
    """El archivo no se pudo convertir en texto (con un motivo para mostrar)."""

def tipo_de_archivo(nombre) -> str | None:
    ext = Path(str(nombre or '')).suffix.lower()
    if ext in PDF:
        return 'pdf'
    if ext in IMAGENES:
        return 'imagen'
    if ext in TEXTOS:
        return 'texto'
    if ext in JSONS:
        return 'json'
    return None

def _alnum(texto) -> int:
    return sum(1 for c in texto or '' if c.isalnum())

def normalizar(texto: str) -> str:
    """Saltos de linea de Windows/Mac y caracteres NUL (pypdf los deja en
    algunos PDF) fuera: el resto del pipeline asume '\\n'."""
    texto = (texto or '').replace('\x00', '')
    return texto.replace('\r\n', '\n').replace('\r', '\n')

def texto_de_pdf(ruta) -> list:
    """La capa de texto de cada pagina ('' en las que no tienen)."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ErrorExtraccion('Falta pypdf: pip install pypdf') from exc
    try:
        lector = PdfReader(str(ruta))
    except Exception as exc:
        raise ErrorExtraccion(f'El PDF no se puede abrir (¿esta dañado o '
                              f'protegido?): {exc}') from exc
    if lector.is_encrypted:
        try:
            lector.decrypt('')
        except Exception:
            pass
    paginas = []
    for pagina in lector.pages:
        try:
            paginas.append(normalizar(pagina.extract_text() or ''))
        except Exception:
            paginas.append('')
    return paginas

def pdf_tiene_texto(paginas) -> bool:
    if not paginas:
        return False
    con_texto = sum(1 for p in paginas if _alnum(p) >= MIN_ALNUM_POR_PAGINA)
    return con_texto / len(paginas) >= PROPORCION_PAGINAS_CON_TEXTO

def decodificar_texto(datos: bytes) -> str:
    """UTF-8 (con o sin BOM) y, si no lo es, Windows-1252: un .txt guardado
    con el Bloc de notas viejo llega asi, y leerlo como UTF-8 rompe cada
    acento."""
    for codificacion in ('utf-8-sig', 'cp1252', 'latin-1'):
        try:
            return normalizar(datos.decode(codificacion))
        except UnicodeDecodeError:
            continue
    return normalizar(datos.decode('utf-8', errors='replace'))

def docs_de_json(datos: bytes) -> list:
    """Documentos en el formato de data/raw/ (export de Paperless).

    Acepta uno solo ({"contenido": ...}), una lista, o {"docs": [...]}. Cada
    uno tiene que traer 'contenido' de texto: sin el no hay nada que indexar,
    y aceptarlo en silencio es justo lo que hace que un documento "cargado"
    no aparezca nunca en el chat."""
    try:
        objeto = json.loads(decodificar_texto(datos))
    except json.JSONDecodeError as exc:
        raise ErrorExtraccion(f'El JSON no es valido: {exc}') from exc
    if isinstance(objeto, dict) and isinstance(objeto.get('docs'), list):
        objeto = objeto['docs']
    docs = objeto if isinstance(objeto, list) else [objeto]
    if not docs:
        raise ErrorExtraccion('El JSON no trae ningun documento.')
    for i, doc in enumerate(docs, 1):
        if not isinstance(doc, dict):
            raise ErrorExtraccion(f'El elemento {i} del JSON no es un objeto.')
        if not isinstance(doc.get('contenido'), str):
            raise ErrorExtraccion(f'El documento {i} del JSON no trae el campo '
                                  f'"contenido" (texto).')
    return docs

def extraer(ruta, tipo, modo='auto', motor_ocr=None, progreso=None,
            paginas_max=0) -> dict:
    """Texto de un PDF, una imagen o un .txt/.md.

    progreso(fase, hecho, total) avisa el avance (fase 'texto' u 'ocr').

    Devuelve {contenido, metodo, paginas, markdown, ocr, avisos}:
      metodo   'texto_pdf' | 'ocr' | 'texto'
      markdown lo que devolvio el OCR, con su formato (para mostrarlo y
               descargarlo); contenido es ese mismo texto sin marcado.
    """
    modo = (modo or 'auto').strip().lower()
    if modo not in MODOS:
        raise ErrorExtraccion(f'Modo desconocido: {modo!r} (auto, ocr o texto).')
    ruta = Path(ruta)
    avisos = []

    if tipo == 'texto':
        crudo = decodificar_texto(ruta.read_bytes())
        if ruta.suffix.lower() == '.md':
            crudo = a_markdown.markdown_a_texto(crudo)
        return {'contenido': crudo.strip(), 'metodo': 'texto', 'paginas': None,
                'markdown': None, 'ocr': None, 'avisos': avisos}

    paginas_texto = []
    if tipo == 'pdf':
        if progreso:
            progreso('texto', 0, 1)
        paginas_texto = texto_de_pdf(ruta)
        if not paginas_texto:
            raise ErrorExtraccion('El PDF no tiene paginas.')
        con_texto = pdf_tiene_texto(paginas_texto)
        if modo == 'texto' or (modo == 'auto' and con_texto):
            contenido = '\n\n'.join(p.strip() for p in paginas_texto if p.strip())
            if _alnum(contenido) < MIN_ALNUM_POR_PAGINA:
                raise ErrorExtraccion(
                    'El PDF no trae capa de texto (es un escaneo): hay que '
                    'leerlo con OCR (modo "auto" u "ocr").')
            if modo == 'texto' and not con_texto:
                avisos.append({'codigo': 'texto_parcial', 'mensaje':
                               'Varias paginas no traen capa de texto y se '
                               'leyo solo la capa existente: esas paginas '
                               'quedan fuera. Con modo "ocr" se leen todas.'})
            return {'contenido': contenido, 'metodo': 'texto_pdf',
                    'paginas': len(paginas_texto), 'markdown': None,
                    'ocr': None, 'avisos': avisos}
    elif tipo != 'imagen':
        raise ErrorExtraccion(f'Tipo de archivo no soportado: {tipo!r}.')
    elif modo == 'texto':
        raise ErrorExtraccion('Una imagen no tiene capa de texto: usa el modo '
                              '"auto" u "ocr".')

    if motor_ocr is None:
        raise ErrorExtraccion(
            'Este archivo necesita OCR (es un escaneo o una imagen) y el OCR no '
            'esta disponible. Con Docker: "docker compose up" levanta el '
            'servicio "ocr". Sin Docker: corre servicio_ocr (ver su README) y '
            'defini OCR_URL=http://127.0.0.1:8001.')

    def avance(hecho, total):
        if progreso:
            progreso('ocr', hecho, total)

    try:
        resultado = motor_ocr.procesar(ruta, paginas_max=paginas_max,
                                       progreso=avance)
    except ErrorOCR as exc:
        raise ErrorExtraccion(str(exc)) from exc
    markdown = resultado.get('markdown') or ''
    contenido = a_markdown.markdown_a_texto(markdown)
    if not contenido.strip():
        raise ErrorExtraccion('El OCR no encontro texto en el archivo.')
    paginas = resultado.get('paginas') or (len(paginas_texto) or None)
    leidas = resultado.get('paginas_leidas')
    if paginas and leidas and leidas < paginas:
        avisos.append({'codigo': 'ocr_recortado', 'mensaje':
                       f'El OCR leyo {leidas} de {paginas} paginas.'})
    return {'contenido': contenido, 'metodo': 'ocr', 'paginas': paginas,
            'markdown': markdown,
            'ocr': {k: resultado.get(k) for k in
                    ('modelo', 'segundos', 'filas_tabla', 'paginas_leidas')},
            'avisos': avisos}

_RE_NOMBRE_SEGURO = re.compile(r'[^A-Za-z0-9._ -]+')

def nombre_seguro(nombre: str, defecto='archivo') -> str:
    """Nombre de archivo sin rutas ni caracteres raros: el nombre lo manda el
    navegador, y '../../algo' no puede decidir donde se escribe."""
    base = Path(str(nombre or '').replace('\\', '/')).name.strip()
    base = _RE_NOMBRE_SEGURO.sub('_', base).strip(' .')
    return base[:120] or defecto
