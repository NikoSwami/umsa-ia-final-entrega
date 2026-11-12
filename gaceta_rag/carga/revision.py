# -*- coding: utf-8 -*-
"""
Lo que se revisa de un documento ANTES de indexarlo.

Cargar un documento "mal" casi nunca falla con un error: queda indexado y el
chat no lo encuentra, o lo cita con el membrete, o lo confunde con otro. Cada
chequeo de aqui es una de esas formas de fallar en silencio, dicha a tiempo:

  sin titulo / titulo sin identidad
      El titulo es de donde salen el organo y el numero (identidad.py). Con
      "acta.pdf" de titulo, "¿sigue vigente la HCF 807/2025?" no la encuentra
      por el numero, y la cita sale con el nombre del archivo.
  duplicado exacto
      El mismo texto ya indexado: dos copias se reparten los cupos del
      contexto y el LLM ve el mismo documento dos veces.
  misma identidad
      Ya hay una HCF 807/2025 en la base. Puede ser la misma resolucion con
      otro OCR (entonces sobra) o un error de numero (entonces la cita va a
      apuntar a la que no es).
  limites del pipeline
      generar_base salta los OCR de mas de 3 MB y corta en 2000 fragmentos. Un
      documento que los pasa entraria ahora en caliente y desapareceria en la
      proxima reconstruccion completa.
  calidad del OCR
      Sin un solo acento en mas de 500 caracteres, el OCR comio los acentos
      (y la busqueda por "resolución" no lo encuentra). El marcador "N°" roto
      es el defecto historico de este corpus. Mucho ruido descartado es un
      escaneo malo.

errores -> bloquean la carga (salvo duplicado, que se puede forzar).
avisos  -> se muestran y se puede seguir.
"""
from __future__ import annotations

import re
import unicodedata

from gaceta_rag import generar_base as gb
from gaceta_rag.mejoras import identidad

ACENTOS = set('áéíóúÁÉÍÓÚñÑüÜ')
MAX_CARACTERES = int(gb.MAX_OCR_MB_DEFAULT * 1024 * 1024)
TIPOS_RESOLUCION = ('resolución', 'resolucion')

_RE_ENCABEZADO = re.compile(
    r"RESOLUCI[OÓ]N\s*(?:N\s*[^\w\s]{0,2}|No\.?|Nro\.?)\s*(\d{1,5})\s*[/\-]\s*((?:19|20)\d{2})",
    re.I)
_RE_ENCABEZADO_CONSEJO = re.compile(
    r"CONSEJO\s+\w+\s+No\.?\s*(\d{1,5})\s*[/\-]\s*((?:19|20)\d{2})", re.I)
_ORGANOS_ENCABEZADO = (
    (re.compile(r"CONSEJO\s+FACULTATIVO", re.I), 'HCF'),
    (re.compile(r"CONSEJO\s+UNIVERSITARIO", re.I), 'HCU'),
    (re.compile(r"CONSEJO\s+DE\s+CARRERA", re.I), 'HCC'),
    (re.compile(r"RESOLUCI[OÓ]N\s+RECTORAL", re.I), 'RR'),
)
_RE_FIN_ENCABEZADO = re.compile(r"VISTOS|CONSIDERANDO", re.I)
VENTANA_ENCABEZADO = 1500

def _aviso(codigo, mensaje, **extra):
    return dict({'codigo': codigo, 'mensaje': mensaje}, **extra)

def es_resolucion(tipo_documento) -> bool:
    return (tipo_documento or '').strip().lower() in TIPOS_RESOLUCION

def identidad_del_encabezado(contenido: str):
    """(organo|None, numero, anio) del encabezado del documento, o None.

    Solo mira el encabezado PROPIO (antes de VISTOS/CONSIDERANDO), para no
    tomar el numero ni el organo de una resolucion citada."""
    cabeza = (contenido or '')[:VENTANA_ENCABEZADO]
    fin = _RE_FIN_ENCABEZADO.search(cabeza)
    if fin:
        cabeza = cabeza[:fin.start()]
    m = _RE_ENCABEZADO.search(cabeza) or _RE_ENCABEZADO_CONSEJO.search(cabeza)
    if not m:
        return None
    organo = None
    for patron, sigla in _ORGANOS_ENCABEZADO:
        if patron.search(cabeza):
            organo = sigla
            break
    return organo, str(int(m.group(1))), m.group(2)

def sugerir_titulo(contenido: str):
    """'Resolución HCF 807/2025' desde el encabezado, o None.

    Es la forma que identidad.identidad_titulo() lee completa (organo, numero
    Y año); los titulos del corpus ('RHCF-807_0001') no traen el año y por eso
    el 80% del corpus no tiene identidad completa desde el titulo."""
    ident = identidad_del_encabezado(contenido)
    if not ident:
        return None
    organo, numero, anio = ident
    if organo:
        return f'Resolución {organo} {numero}/{anio}'
    return f'Resolución {numero}/{anio}'

def organo_a_etiquetas(organo) -> list:
    return [organo] if organo else []

def _caracteres_raros(texto: str) -> str:
    raros = sorted({c for c in texto
                    if unicodedata.category(c) in ('Co', 'Cn', 'Cs')
                    or c == '�'})
    return ''.join(raros)

def fragmentos(contenido: str, doc: dict, doc_id: str = 'doc_000000') -> list:
    """Los chunks que generaria el pipeline, con su misma limpieza y cabecera."""
    limpio = gb.limpiar_ocr_seguro(contenido or '')
    if not limpio or len(limpio) < gb.CHUNK_MIN_LEN:
        return []
    return gb.dividir_chunks(limpio, gb.cabecera_enriquecida(doc), doc_id)

def revisar(doc: dict, *, buscar_huella=None, buscar_identidad=None,
            doc_id=None, permitir_duplicado=False) -> dict:
    """Revisa un raw doc ({contenido, titulo, tipo_documento, ...}).

    buscar_huella(h)               -> doc_id ya indexado con ese contenido, o None
    buscar_identidad(org, num, anio) -> [(doc_id, titulo), ...] con esa identidad
    doc_id                         el del documento revisado (si ya tiene uno):
                                   no es duplicado de si mismo.
    """
    contenido = doc.get('contenido') or ''
    titulo = (doc.get('titulo') or '').strip()
    errores, avisos, duplicados = [], [], []

    limpio = gb.limpiar_ocr_seguro(contenido) if contenido else ''
    chunks = fragmentos(contenido, doc, doc_id or 'doc_000000')
    acentos = sum(1 for c in contenido if c in ACENTOS)
    stats = {
        'caracteres': len(contenido),
        'caracteres_indexables': len(limpio),
        'fragmentos': len(chunks),
        'acentos': acentos,
        'filas_tabla': sum(1 for l in contenido.splitlines()
                           if l.lstrip().startswith('|')),
        'paginas': doc.get('nroPaginas') or None,
    }

    if not contenido.strip():
        errores.append(_aviso('sin_texto', 'El documento no tiene texto.'))
    elif not chunks:
        errores.append(_aviso(
            'sin_fragmentos',
            'Despues de limpiar el ruido no queda texto suficiente para '
            f'indexar (minimo {gb.CHUNK_MIN_LEN} caracteres utiles).'))
    if len(contenido) > MAX_CARACTERES:
        errores.append(_aviso(
            'demasiado_grande',
            f'El texto tiene {len(contenido):,} caracteres; el pipeline salta '
            f'los de mas de {MAX_CARACTERES:,} (generar_base.py) y en la '
            'proxima reconstruccion desapareceria. Dividilo en partes.'))
    if not titulo:
        errores.append(_aviso(
            'sin_titulo', 'Falta el titulo: es lo que se muestra en cada cita.'))

    huella = gb.hash_contenido(contenido)
    if buscar_huella and contenido.strip():
        igual = buscar_huella(huella)
        if igual and igual != doc_id:
            duplicados.append({'doc_id': igual, 'motivo': 'mismo_texto'})
            (avisos if permitir_duplicado else errores).append(_aviso(
                'duplicado_exacto',
                f'Ese mismo texto ya esta indexado como {igual}. Dos copias se '
                'reparten el contexto del chat. Si de verdad queres otra '
                'copia, marca "cargar aunque este duplicado".',
                doc_id=igual))

    organo, numero = identidad.identidad_titulo(titulo, contenido)
    encabezado = identidad_del_encabezado(contenido)
    sugerido = sugerir_titulo(contenido)
    if titulo and es_resolucion(doc.get('tipo_documento')):
        if not organo or not numero:
            avisos.append(_aviso(
                'sin_identidad',
                'El titulo no tiene la forma "Resolución HCF 123/2024": el chat '
                'no va a poder encontrarla por su numero ni citarla con el.'
                + (f' Sugerido: "{sugerido}".' if sugerido else ''),
                sugerido=sugerido))
        elif encabezado:
            org_enc, num_enc, anio_enc = encabezado
            if numero != f'{num_enc}/{anio_enc}' or (org_enc and org_enc != organo):
                avisos.append(_aviso(
                    'identidad_distinta',
                    f'El titulo dice {organo} {numero} pero el encabezado del '
                    f'texto dice {org_enc or "?"} {num_enc}/{anio_enc}. '
                    'Revisa cual es el correcto.'))
    if organo and numero and buscar_identidad:
        num, anio = numero.split('/')
        otros = [(d, t) for d, t in buscar_identidad(organo, num, anio)
                 if d != doc_id]
        if otros:
            lista = ', '.join(f'{d} ({t})' if t else d for d, t in otros[:5])
            duplicados += [{'doc_id': d, 'motivo': 'misma_identidad'}
                           for d, _ in otros]
            avisos.append(_aviso(
                'misma_identidad',
                f'Ya hay una {organo} {numero} en la base: {lista}. Si es la '
                'misma resolucion, sobra; si es otra, revisa el numero.'))

    if len(contenido) > 500 and acentos == 0:
        avisos.append(_aviso(
            'sin_acentos',
            'Texto en español sin un solo acento: el OCR probablemente los '
            'perdio, y la busqueda por palabras con tilde no lo va a encontrar.'))
    if contenido and gb.arreglar_marcador_resolucion(contenido) != contenido:
        avisos.append(_aviso(
            'marcador_corregido',
            'El OCR leyo mal el "N°" de alguna resolucion (N*, N\'...). Se '
            'corrige solo al indexar; no hace falta tocarlo.'))
    raros = _caracteres_raros(contenido)
    if raros:
        avisos.append(_aviso(
            'caracteres_raros',
            f'Hay caracteres que no son texto ({raros[:20]!r}): posible '
            'corrupcion del OCR o de la codificacion.'))
    if len(contenido) > 1000 and len(limpio) < 0.6 * len(contenido.strip()):
        avisos.append(_aviso(
            'mucho_ruido',
            f'Se descarta {100 - 100 * len(limpio) // max(1, len(contenido.strip()))}% '
            'del texto como ruido de OCR: el escaneo puede ser malo.'))
    if len(chunks) >= gb.MAX_CHUNKS_DOC:
        avisos.append(_aviso(
            'demasiados_fragmentos',
            f'Solo se indexan los primeros {gb.MAX_CHUNKS_DOC} fragmentos: el '
            'final del documento no va a ser buscable. Dividilo en partes.'))
    elif contenido.strip() and len(limpio) < 300:
        avisos.append(_aviso(
            'muy_corto',
            f'Solo {len(limpio)} caracteres utiles: ¿el archivo es el correcto?'))

    return {
        'ok': not errores,
        'errores': errores,
        'avisos': avisos,
        'identidad': {'organo': organo, 'numero': numero},
        'encabezado': ({'organo': encabezado[0],
                        'numero': f'{encabezado[1]}/{encabezado[2]}'}
                       if encabezado else None),
        'titulo_sugerido': sugerido,
        'huella': huella,
        'duplicados': duplicados,
        'estadisticas': stats,
    }
