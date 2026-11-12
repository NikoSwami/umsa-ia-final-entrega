# -*- coding: utf-8 -*-
"""
OCR LIVIANO: los mismos modelos de la familia PaddleOCR, exportados a ONNX y
corridos con onnxruntime, sin Paddle ni un modelo de lenguaje-vision.

  RapidOCR     deteccion PP-OCRv5 + reconocedor LATINO PP-OCRv5 (acentos, ñ);
               dentro de las tablas, lo corto o dudoso se relee con PP-OCRv6
  RapidLayout  PP-DocLayoutV3, el mismo detector de diseño de PaddleOCR-VL:
               aqui solo se usa para ubicar las TABLAS
  RapidTable   SLANet+ para la estructura de cada tabla (con celdas
               combinadas: rowspan/colspan)

Medido en CPU contra PaddleOCR-VL-1.5 sobre cuatro paginas escaneadas con
verdad conocida (una resolucion; un anexo con tabla con y sin bordes; la tabla
real de costos; un anexo con encabezado de dos niveles, celdas de varios
renglones, celdas vacias y una tabla sin bordes). 144 celdas en total:

                      VL-1.5            liviano
  carga del modelo    ~95-110 s         ~3-5 s
  por pagina          ~400-780 s        ~5-7 s (laptop libre)
  memoria (pico)      ~8,5 GB           ~0,8 GB
  celdas correctas    141/144           143/144
  (VL: le quita la tilde a "Maestría"; liviano: no lee un guion "—" suelto)

Tablas SIN bordes: SLANet+ junta columnas ("12 5" en una celda). Si la region
no tiene lineas verticales, manda la grilla armada con la posicion de cada
palabra (filas por altura, columnas por los huecos verticales, y un renglon
pegado al anterior con casi todo vacio es la continuacion de su celda), salvo
que SLANet+ vea mas columnas. Las tablas con bordes las resuelve SLANet+.

Este modulo no importa onnxruntime ni opencv al cargarse: la API de la Gaceta
lo puede importar sin tenerlos (solo el servicio OCR los instala).
"""
from __future__ import annotations

import os
import re
import tempfile
import threading
import time

from . import a_markdown
from .ocr import EXTENSIONES, ErrorOCR, paginas_de_pdf, recortar_pdf

DPI_MIN, DPI_MAX, DPI_SIN_IMAGEN = 150, 300, 200
REC_DEFECTO = 'v5'

def reconocedor() -> str:
    """OCR_REC: v5 (PP-OCRv5 latino mobile) | v6 (PP-OCRv6 small)."""
    return (os.environ.get('OCR_REC') or REC_DEFECTO).strip().lower()

def dpi_de_imagenes(imagenes, minimo=None) -> int:
    """imagenes: [(ancho_px, ancho_pt)] de una pagina -> el dpi a usar.

    Manda la imagen que mas ancho ocupa en la pagina (el escaneo, no un logo)."""
    minimo = minimo or int(os.environ.get('OCR_DPI_MIN') or DPI_MIN)
    validas = [(px, pt) for px, pt in imagenes if px and pt and pt > 0]
    if not validas:
        return max(DPI_SIN_IMAGEN, minimo)
    px, pt = max(validas, key=lambda i: i[1])
    return int(round(min(max(px / (pt / 72), minimo), max(DPI_MAX, minimo))))

def dpi_de_pagina(pagina) -> int:
    fijo = (os.environ.get('OCR_DPI') or 'auto').strip().lower()
    if fijo != 'auto':
        return int(fijo)
    try:
        import pypdfium2.raw as pdfium_c
        imagenes = []
        for o in pagina.get_objects(filter=(pdfium_c.FPDF_PAGEOBJ_IMAGE,)):
            izq, _, der, _ = o.get_bounds()
            imagenes.append((o.get_px_size()[0], der - izq))
        return dpi_de_imagenes(imagenes)
    except Exception:
        return DPI_SIN_IMAGEN
DET_LIMITE = 1280
DET_UNCLIP = 1.3
UMBRAL_TEXTO = 0.5
LARGO_RELEER = 3

def grilla_por_posiciones(palabras):
    """Tabla sin bordes a partir de la posicion de cada palabra.

    palabras: [(x0, y0, x1, y1, texto)] en coordenadas de la region.
    Filas: palabras cuya altura central esta a menos de media altura de letra.
    Columnas: bandas horizontales que ninguna palabra cruza (un hueco de mas
    de una altura de letra separa columnas; el espacio entre palabras de una
    misma celda es bastante menor).
    Devuelve [[celda, ...], ...] o None si no salen al menos 2 columnas."""
    if len(palabras) < 4:
        return None
    alturas = sorted(p[3] - p[1] for p in palabras)
    hm = float(alturas[len(alturas) // 2]) or 1.0

    filas = []
    for p in sorted(palabras, key=lambda p: (p[1] + p[3]) / 2):
        yc = (p[1] + p[3]) / 2
        if filas and abs(yc - filas[-1]['y']) <= 0.5 * hm:
            filas[-1]['p'].append(p)
            filas[-1]['y'] = sum((q[1] + q[3]) / 2 for q in filas[-1]['p']) / len(filas[-1]['p'])
        else:
            filas.append({'y': yc, 'p': [p]})

    bandas = []
    for x0, x1 in sorted((p[0], p[2]) for p in palabras):
        if bandas and x0 <= bandas[-1][1] + 1.0 * hm:
            bandas[-1][1] = max(bandas[-1][1], x1)
        else:
            bandas.append([x0, x1])
    if len(bandas) < 2:
        return None

    def banda_de(xc):
        return min(range(len(bandas)), key=lambda k: 0 if bandas[k][0] <= xc <= bandas[k][1]
                   else min(abs(xc - bandas[k][0]), abs(xc - bandas[k][1])))

    grilla, y_previa = [], None
    for f in filas:
        celdas = [[] for _ in bandas]
        for p in sorted(f['p'], key=lambda p: p[0]):
            celdas[banda_de((p[0] + p[2]) / 2)].append(p[4])
        fila = [' '.join(c) for c in celdas]
        continua = (grilla and y_previa is not None
                    and f['y'] - y_previa < 1.7 * hm
                    and sum(1 for c in fila if c) <= len(fila) // 2)
        if continua:
            grilla[-1] = [f'{a} {b}'.strip() if b else a
                          for a, b in zip(grilla[-1], fila)]
        else:
            grilla.append(fila)
        y_previa = f['y']
    return grilla

def grilla_a_markdown(grilla) -> str:
    def celda(t):
        return str(t).replace('|', r'\|')
    lineas = ['| ' + ' | '.join(celda(c) for c in grilla[0]) + ' |',
              '|' + ' --- |' * len(grilla[0])]
    lineas += ['| ' + ' | '.join(celda(c) for c in f) + ' |' for f in grilla[1:]]
    return '\n'.join(lineas)

def columnas_html(html: str) -> int:
    filas = re.findall(r'<tr\b[^>]*>(.*?)</tr>', html or '', re.S | re.I)
    return max((len(re.findall(r'<t[dh]\b', f, re.I)) for f in filas), default=0)

def ordenar_bloques(bloques) -> str:
    """[(y, tipo, texto)] -> Markdown en orden de lectura.

    Renglones de texto seguidos van en lineas consecutivas; una tabla queda
    separada por lineas en blanco, y tambien el texto que esta lejos del
    renglon anterior (mas de 1,5 veces la altura de un renglon): un parrafo."""
    salida, previo = [], None
    for y, tipo, texto, alto in sorted(bloques, key=lambda b: b[0]):
        if tipo == 'tabla':
            salida.append('\n\n' + texto + '\n\n')
            previo = None
            continue
        if previo is not None and y - previo < 1.5 * max(alto, 1):
            salida.append('\n' + texto)
        else:
            salida.append('\n\n' + texto)
        previo = y
    return re.sub(r'\n{3,}', '\n\n', ''.join(salida)).strip()

class MotorOCRLiviano:
    """Misma interfaz que ocr.MotorOCRPaddle: cargar(), procesar(), estado()."""

    tipo = 'local'
    modelo = 'Liviano: PP-OCRv5 latino + PP-DocLayoutV3 + SLANet+ (ONNX)'

    def __init__(self):
        self._motores = None
        self._lock = threading.Lock()
        self.cargando = False
        self.error = ''
        self.dispositivo = 'cpu'

    @property
    def listo(self) -> bool:
        return self._motores is not None

    def estado(self) -> dict:
        return {'disponible': True, 'tipo': self.tipo, 'listo': self.listo,
                'cargando': self.cargando, 'error': self.error,
                'modelo': self.modelo, 'dispositivo': self.dispositivo}

    def cargar(self):
        with self._lock:
            if self._motores is not None:
                return self._motores
            self.cargando, self.error = True, ''
            try:
                from rapidocr import LangDet, LangRec, ModelType, OCRVersion, RapidOCR
                from rapid_layout import ModelType as TipoDiseno, RapidLayout, RapidLayoutInput
                from rapid_table import ModelType as TipoTabla, RapidTable, RapidTableInput
            except Exception as exc:
                self.cargando = False
                self.error = f'falta el OCR liviano (rapidocr/rapid-table): {exc}'
                raise ErrorOCR(self.error) from exc
            try:
                params = {
                    'Det.ocr_version': OCRVersion.PPOCRV5, 'Det.lang_type': LangDet.CH,
                    'Det.model_type': ModelType.MOBILE,
                    'Det.limit_side_len': DET_LIMITE, 'Det.limit_type': 'max',
                    'Det.unclip_ratio': DET_UNCLIP,
                }
                if reconocedor() == 'v5':
                    params.update({'Rec.ocr_version': OCRVersion.PPOCRV5,
                                   'Rec.lang_type': LangRec.LATIN,
                                   'Rec.model_type': ModelType.MOBILE})
                else:
                    params.update({'Rec.ocr_version': OCRVersion.PPOCRV6,
                                   'Rec.model_type': ModelType.SMALL})
                ocr = RapidOCR(params=params)
                diseno = RapidLayout(RapidLayoutInput(model_type=TipoDiseno.PP_DOC_LAYOUTV3))
                tabla = RapidTable(RapidTableInput(model_type=TipoTabla.SLANETPLUS,
                                                   use_ocr=True, ocr_params=params))
                fino = RapidOCR(params={'Rec.ocr_version': OCRVersion.PPOCRV6,
                                        'Rec.model_type': ModelType.SMALL})
                for paso in ('_load_det_model', '_load_cls_model', '_load_rec_model'):
                    getattr(ocr, paso, lambda: None)()
                getattr(fino, '_load_rec_model', lambda: None)()
                self._motores = (ocr, diseno, tabla, fino)
                return self._motores
            except Exception as exc:
                self.error = f'{type(exc).__name__}: {exc}'
                raise ErrorOCR(f'No se pudo cargar el OCR liviano: {self.error}') from exc
            finally:
                self.cargando = False

    def _pagina(self, img) -> str:
        import numpy as np

        ocr, diseno, tabla, fino = self._motores
        d = diseno(img)
        regiones = []
        if d.boxes is not None:
            regiones = [tuple(int(v) for v in b) for b, c in zip(d.boxes, d.class_names)
                        if str(c).lower() == 'table']

        def dentro(caja, r):
            cx, cy = caja[:, 0].mean(), caja[:, 1].mean()
            return r[0] <= cx <= r[2] and r[1] <= cy <= r[3]

        o = ocr(img, return_word_box=True, text_score=0.0)
        crudas = list(zip(o.boxes, o.txts, o.scores)) if o.boxes is not None else []
        por_palabra = o.word_results if (o.word_results
                                         and len(o.word_results) == len(crudas)) else [()] * len(crudas)
        cajas, palabras = [], []
        for (caja, txt, conf), ws in zip(crudas, por_palabra):
            if (fino is not None and any(dentro(caja, r) for r in regiones)
                    and (len(txt.strip()) <= LARGO_RELEER or conf < UMBRAL_TEXTO)):
                txt2, conf2 = _releer(fino, img, caja)
                if txt2.strip() and conf2 >= UMBRAL_TEXTO:
                    txt, conf, ws = txt2, conf2, ()
            if conf < UMBRAL_TEXTO or not txt.strip():
                continue
            cajas.append((caja, txt, conf))
            propias = []
            for w in ws or ():
                if w and len(w) > 2 and w[2] is not None:
                    pts = np.asarray(w[2], dtype=float).reshape(-1, 2)
                    propias.append((pts[:, 0].min(), pts[:, 1].min(),
                                    pts[:, 0].max(), pts[:, 1].max(), w[0]))
            palabras.append(propias or [(caja[:, 0].min(), caja[:, 1].min(),
                                         caja[:, 0].max(), caja[:, 1].max(), txt)])

        bloques, usadas = [], set()
        for r in regiones:
            x0, y0, x1, y1 = r
            propias = [i for i, c in enumerate(cajas) if dentro(c[0], r)]
            if not propias:
                continue
            usadas.update(propias)
            recorte = img[y0:y1, x0:x1]
            rel = np.array([cajas[i][0] - [x0, y0] for i in propias], dtype=np.float32)
            res = tabla(recorte, ocr_results=[(rel, tuple(cajas[i][1] for i in propias),
                                               tuple(cajas[i][2] for i in propias))])
            html = res.pred_htmls[0] if res.pred_htmls else ''
            md = a_markdown.a_markdown(html, imagenes=False) if html else ''
            if _lineas_verticales(recorte) < 2:
                grilla = grilla_por_posiciones(
                    [(p[0] - x0, p[1] - y0, p[2] - x0, p[3] - y0, p[4])
                     for i in propias for p in palabras[i]])
                if grilla and len(grilla[0]) >= columnas_html(html):
                    md = grilla_a_markdown(grilla)
            if not md:
                md = '\n'.join(cajas[i][1] for i in propias)
            bloques.append((y0, 'tabla', md, 0))
        for i, (caja, txt, _) in enumerate(cajas):
            if i not in usadas and txt.strip():
                bloques.append((float(caja[:, 1].min()), 'texto', txt,
                                float(caja[:, 1].max() - caja[:, 1].min())))
        return ordenar_bloques(bloques)

    def _imagenes(self, ruta):
        """Cada pagina como arreglo RGB (un PDF se rasteriza a la resolucion de
        su escaneo: dpi_de_pagina)."""
        import numpy as np

        if str(ruta).lower().endswith('.pdf'):
            import pypdfium2 as pdfium
            doc = pdfium.PdfDocument(str(ruta))
            try:
                for pagina in doc:
                    escala = dpi_de_pagina(pagina) / 72
                    yield np.array(pagina.render(scale=escala).to_pil().convert('RGB'))
            finally:
                doc.close()
        else:
            from PIL import Image
            with Image.open(ruta) as im:
                yield np.array(im.convert('RGB'))

    def procesar(self, ruta, paginas_max=0, progreso=None) -> dict:
        ruta = str(ruta)
        if not ruta.lower().endswith(EXTENSIONES):
            raise ErrorOCR(f'Formato no soportado por el OCR: {os.path.basename(ruta)}')
        self.cargar()
        with self._lock, tempfile.TemporaryDirectory(prefix='ocr_') as tmp:
            entrada, total = recortar_pdf(ruta, paginas_max, tmp)
            if total is None:
                total = paginas_de_pdf(entrada) or 1
            leidas = min(total, paginas_max) if paginas_max else total
            t0 = time.time()
            piezas = []
            try:
                for i, img in enumerate(self._imagenes(entrada), 1):
                    piezas.append(self._pagina(img))
                    if progreso:
                        progreso(i, leidas)
            except ErrorOCR:
                raise
            except Exception as exc:
                raise ErrorOCR(f'El OCR fallo en la pagina {len(piezas) + 1}: '
                               f'{type(exc).__name__}: {exc}') from exc
            markdown = '\n\n'.join(p for p in piezas if p.strip())
            return {
                'markdown': markdown,
                'paginas': total,
                'paginas_leidas': len(piezas),
                'segundos': round(time.time() - t0, 1),
                'filas_tabla': a_markdown.contar_filas(markdown),
                'modelo': self.modelo,
            }

def _releer(fino, img, caja):
    """Vuelve a reconocer una caja ampliada al doble con el reconocedor fino.
    Un caracter suelto en un recorte angosto confunde al modelo; ampliado,
    "9" deja de leerse "6"."""
    import cv2

    x0, x1 = int(caja[:, 0].min()), int(caja[:, 0].max())
    y0, y1 = int(caja[:, 1].min()), int(caja[:, 1].max())
    recorte = img[max(0, y0 - 3):y1 + 3, max(0, x0 - 3):x1 + 3]
    if recorte.size == 0:
        return '', 0.0
    recorte = cv2.resize(recorte, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    r = fino(recorte, use_det=False, use_cls=False, use_rec=True)
    if not r.txts:
        return '', 0.0
    return r.txts[0], float(r.scores[0])

def _lineas_verticales(recorte) -> int:
    """Cuantas lineas verticales largas (bordes) tiene una region."""
    import cv2

    gris = cv2.cvtColor(recorte, cv2.COLOR_RGB2GRAY)
    bn = cv2.adaptiveThreshold(255 - gris, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                               cv2.THRESH_BINARY, 15, -2)
    alto = recorte.shape[0]
    nucleo = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(10, alto // 4)))
    vert = cv2.morphologyEx(bn, cv2.MORPH_OPEN, nucleo)
    _, _, est, _ = cv2.connectedComponentsWithStats(vert)
    return sum(1 for e in est[1:] if e[cv2.CC_STAT_HEIGHT] >= 0.5 * alto)
