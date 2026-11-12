# -*- coding: utf-8 -*-
"""
OCR de PDFs e imagenes. Tres motores con la misma interfaz,
`procesar(ruta, progreso=...) -> dict`:

  MotorOCRLiviano  (ocr_liviano.py, por defecto) PP-OCRv5 latino +
                   PP-DocLayoutV3 + SLANet+ en ONNX: ~15 s y <1 GB por pagina
                   en CPU, tablas incluidas.
  MotorOCRPaddle   PaddleOCR-VL, el de
                   PaddleOCR-VL
                   (local/ocr_local.py): ~5 min y ~8 GB por pagina en CPU.
                   Para la GPU.
  MotorOCRRemoto   le pasa el archivo al servicio OCR (servicio_ocr/app.py,
                   el contenedor "ocr", que corre uno de los dos de arriba)
                   por HTTP y espera. Es lo que usa la API de la Gaceta: asi
                   el chat no comparte memoria ni dependencias con el OCR.

motor_local() elige entre los dos locales con OCR_MOTOR (liviano | vl).
motor_desde_entorno() elige: OCR_URL -> remoto; OCR_LOCAL=1 -> local;
nada -> sin OCR (los PDF con texto igual se cargan; los escaneados no).

Este modulo NO importa Paddle ni numpy al cargarse: el servicio OCR y la API
lo comparten, y la API no tiene Paddle instalado.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path

from . import a_markdown

EXTENSIONES = ('.pdf', '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.webp')
VERSION_DEFECTO = 'v1.5'

class ErrorOCR(RuntimeError):
    """El OCR no pudo leer el archivo (o no esta disponible)."""

def paginas_de_pdf(ruta) -> int | None:
    """Cuantas paginas tiene un PDF; None si no es PDF o no se puede leer."""
    if not str(ruta).lower().endswith('.pdf'):
        return None
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(ruta)).pages)
    except Exception:
        return None

def recortar_pdf(ruta, maximo, destino):
    """Las primeras `maximo` paginas en un PDF aparte (0 = no recortar).
    Igual que en ocr_local.py: sirve para probar un lote sin esperar una hora."""
    if not maximo or not str(ruta).lower().endswith('.pdf'):
        return str(ruta), None
    from pypdf import PdfReader, PdfWriter

    lector = PdfReader(str(ruta))
    total = len(lector.pages)
    if total <= maximo:
        return str(ruta), total
    escritor = PdfWriter()
    for pagina in lector.pages[:maximo]:
        escritor.add_page(pagina)
    corto = os.path.join(destino, f'_recorte_{os.path.basename(ruta)}')
    with open(corto, 'wb') as f:
        escritor.write(f)
    return corto, total

def _markdown_de_resultado(res, carpeta) -> str:
    """El Markdown de UNA pagina.

    PaddleOCR 3.x lo expone en res.markdown['markdown_texts']. Si una version
    no lo trae, se cae a lo que hacia ocr_local.py: guardarlo y leerlo.
    Leerlo del objeto evita ademas el orden de archivos: ocr_local.py unia las
    piezas con sorted(glob), y 'x_10.md' queda antes que 'x_2.md'."""
    try:
        md = res.markdown
        if isinstance(md, dict):
            texto = md.get('markdown_texts')
            if isinstance(texto, str):
                return texto
    except Exception:
        pass
    os.makedirs(carpeta, exist_ok=True)
    antes = set(Path(carpeta).rglob('*.md'))
    res.save_to_markdown(save_path=carpeta)
    nuevos = sorted(set(Path(carpeta).rglob('*.md')) - antes)
    return '\n\n'.join(p.read_text(encoding='utf-8') for p in nuevos)

class MotorOCRPaddle:
    """PaddleOCR-VL en este proceso. Carga perezosa y una lectura a la vez:
    el modelo no es reentrante y dos PDFs en paralelo duplican la memoria.

    Cargar y leer tienen que pasar en el MISMO hilo: Paddle guarda su modo de
    ejecucion por hilo, y un pipeline cargado en un hilo falla en otro ("int
    (Tensor) is not supported in static graph mode"). Quien lo usa desde un
    hilo de trabajo (servicio_ocr, el Cargador con OCR_LOCAL=1) lo carga ahi
    mismo: cargar() perezoso dentro de procesar() ya lo asegura."""

    tipo = 'local'

    def __init__(self, version=None, dispositivo=None):
        self.version = version or os.environ.get('OCR_VERSION') or VERSION_DEFECTO
        disp = (dispositivo or os.environ.get('OCR_DISPOSITIVO') or '').strip().lower()
        self.dispositivo = disp if disp in ('cpu', 'gpu') else None
        self._pipeline = None
        self._lock = threading.Lock()
        self.cargando = False
        self.error = ''

    @property
    def modelo(self) -> str:
        return f'PaddleOCR-VL {self.version}'

    @property
    def listo(self) -> bool:
        return self._pipeline is not None

    def estado(self) -> dict:
        return {'disponible': True, 'tipo': self.tipo, 'listo': self.listo,
                'cargando': self.cargando, 'error': self.error,
                'modelo': self.modelo, 'dispositivo': self.dispositivo or 'auto'}

    def cargar(self):
        """Carga el pipeline. Mismos intentos que ocr_local.cargar_pipeline:
        con device si se pidio, y sin el si esta version no lo acepta."""
        with self._lock:
            if self._pipeline is not None:
                return self._pipeline
            self.cargando, self.error = True, ''
            try:
                from paddleocr import PaddleOCRVL
            except Exception as exc:
                self.cargando = False
                self.error = f'paddleocr no esta instalado: {exc}'
                raise ErrorOCR(self.error) from exc
            intentos = [{'pipeline_version': self.version}]
            if self.dispositivo:
                intentos.insert(0, {'pipeline_version': self.version,
                                    'device': self.dispositivo})
            ultimo = None
            try:
                for kwargs in intentos:
                    try:
                        self._pipeline = PaddleOCRVL(**kwargs)
                        return self._pipeline
                    except TypeError as exc:
                        ultimo = exc
                raise ultimo
            except Exception as exc:
                self.error = f'{type(exc).__name__}: {exc}'
                raise ErrorOCR(f'No se pudo cargar {self.modelo}: {self.error}') from exc
            finally:
                self.cargando = False

    def procesar(self, ruta, paginas_max=0, progreso=None) -> dict:
        """OCR de un PDF o imagen.

        progreso(pagina_hecha, paginas_total) se llama al terminar cada pagina.
        Devuelve {markdown, paginas, paginas_leidas, segundos, filas_tabla,
        modelo}. El Markdown ya viene limpio (tablas de pipes, sin HTML ni
        imagenes): es lo que ocr_local.py llama formato 'md' --sin-imagenes.
        """
        ruta = str(ruta)
        if not ruta.lower().endswith(EXTENSIONES):
            raise ErrorOCR(f'Formato no soportado por el OCR: {os.path.basename(ruta)}')
        pipeline = self.cargar()
        with self._lock, tempfile.TemporaryDirectory(prefix='ocr_') as tmp:
            entrada, total = recortar_pdf(ruta, paginas_max, tmp)
            if total is None:
                total = paginas_de_pdf(entrada) or 1
            leidas = min(total, paginas_max) if paginas_max else total
            t0 = time.time()
            piezas = []
            iterar = getattr(pipeline, 'predict_iter', None) or pipeline.predict
            try:
                for i, res in enumerate(iterar(entrada), 1):
                    piezas.append(_markdown_de_resultado(
                        res, os.path.join(tmp, f'pagina_{i:04d}')))
                    if progreso:
                        progreso(i, leidas)
            except ErrorOCR:
                raise
            except Exception as exc:
                raise ErrorOCR(f'El OCR fallo en la pagina {len(piezas) + 1}: '
                               f'{type(exc).__name__}: {exc}') from exc
            limpio = [a_markdown.a_markdown(p, imagenes=False) for p in piezas]
            markdown = '\n\n'.join(p for p in limpio if p.strip())
            return {
                'markdown': markdown,
                'paginas': total,
                'paginas_leidas': len(piezas),
                'segundos': round(time.time() - t0, 1),
                'filas_tabla': a_markdown.contar_filas(markdown),
                'modelo': self.modelo,
            }

class MotorOCRRemoto:
    """Cliente del servicio OCR (servicio_ocr/app.py).

    El OCR de un PDF largo en CPU tarda minutos: en vez de una peticion que
    espera todo ese tiempo (y que un proxy corta), se sube el archivo, el
    servicio devuelve un id y se le pregunta cada pocos segundos por cual
    pagina va. Eso es tambien lo que deja mostrar el avance en /carga."""

    tipo = 'remoto'

    def __init__(self, url, timeout=None, intervalo=2.0, cliente=None):
        self.url = url.rstrip('/')
        self.timeout = float(timeout or os.environ.get('OCR_TIMEOUT_S') or 3600)
        self.intervalo = intervalo
        self._cliente = cliente

    def _http(self):
        if self._cliente is None:
            import httpx
            self._cliente = httpx.Client(timeout=60.0)
        return self._cliente

    def estado(self) -> dict:
        try:
            r = self._http().get(f'{self.url}/salud', timeout=5.0)
            r.raise_for_status()
            datos = r.json()
            return dict(datos, disponible=True, tipo=self.tipo, url=self.url)
        except Exception as exc:
            return {'disponible': False, 'tipo': self.tipo, 'url': self.url,
                    'error': f'el servicio OCR no responde: {exc}'}

    def procesar(self, ruta, paginas_max=0, progreso=None) -> dict:
        ruta = Path(ruta)
        try:
            with open(ruta, 'rb') as f:
                r = self._http().post(
                    f'{self.url}/ocr',
                    files={'archivo': (ruta.name, f, 'application/octet-stream')},
                    data={'paginas_max': str(int(paginas_max or 0))})
        except Exception as exc:
            raise ErrorOCR(f'No se pudo enviar el archivo al servicio OCR '
                           f'({self.url}): {exc}') from exc
        if r.status_code >= 400:
            raise ErrorOCR(f'El servicio OCR rechazo el archivo: {_detalle(r)}')
        trabajo = r.json()['id']

        limite = time.time() + self.timeout
        fallos = 0
        try:
            while True:
                try:
                    r = self._http().get(f'{self.url}/ocr/{trabajo}')
                    r.raise_for_status()
                    estado = r.json()
                    fallos = 0
                except Exception as exc:
                    fallos += 1
                    if fallos >= 5:
                        raise ErrorOCR(f'Se perdio el contacto con el servicio '
                                       f'OCR: {exc}') from exc
                    time.sleep(self.intervalo)
                    continue
                if progreso and estado.get('paginas'):
                    progreso(estado.get('pagina') or 0, estado['paginas'])
                if estado.get('estado') == 'listo':
                    return estado['resultado']
                if estado.get('estado') == 'error':
                    raise ErrorOCR(estado.get('error') or 'error desconocido del OCR')
                if time.time() > limite:
                    raise ErrorOCR(f'El OCR tardo mas de {self.timeout:.0f} s.')
                time.sleep(self.intervalo)
        finally:
            try:
                self._http().delete(f'{self.url}/ocr/{trabajo}')
            except Exception:
                pass

def _detalle(respuesta) -> str:
    try:
        return str(respuesta.json().get('detail') or respuesta.text)[:300]
    except Exception:
        return f'HTTP {respuesta.status_code}'

MOTORES_LOCALES = ('liviano', 'vl')
MOTOR_DEFECTO = 'liviano'

def motor_local(nombre=None):
    """El motor que corre en ESTE proceso, segun OCR_MOTOR.

    liviano  PP-OCRv5 + PP-DocLayoutV3 + SLANet+ en ONNX (ocr_liviano.py):
             ~15 s y <1 GB por pagina en CPU. Por defecto.
    vl       PaddleOCR-VL: el modelo de lenguaje-vision del proyecto original;
             ~5 min y ~8 GB por pagina en CPU. Tiene sentido con GPU."""
    nombre = (nombre or os.environ.get('OCR_MOTOR') or MOTOR_DEFECTO).strip().lower()
    if nombre == 'vl':
        return MotorOCRPaddle()
    if nombre == 'liviano':
        from .ocr_liviano import MotorOCRLiviano
        return MotorOCRLiviano()
    raise ValueError(f'OCR_MOTOR desconocido: {nombre!r} '
                     f'(opciones: {", ".join(MOTORES_LOCALES)}).')

def motor_desde_entorno():
    """OCR_URL -> el servicio; OCR_LOCAL=1 -> en este proceso; si no, None."""
    url = (os.environ.get('OCR_URL') or '').strip()
    if url:
        return MotorOCRRemoto(url)
    if (os.environ.get('OCR_LOCAL') or '').strip().lower() in ('1', 'true', 'si', 'sí', 'on'):
        return motor_local()
    return None
